import asyncio
import uuid
from datetime import timedelta
from typing import Any

import opentelemetry.context
import opentelemetry.trace
import pytest
from agents import (
    Agent,
    Runner,
    Span,
    Trace,
    TracingProcessor,
    custom_span,
    function_tool,
    trace,
)
from agents.tracing import get_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from openinference.instrumentation.openai_agents._processor import (
    OpenInferenceTracingProcessor,
)
from openinference.semconv.trace import SpanAttributes
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.openai_agents import _temporal_openai_agents
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    ResponseBuilders,
    TestModel,
)
from temporalio.contrib.opentelemetry import create_tracer_provider
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
)
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)
from tests.contrib.openai_agents.test_openai import (
    ResearchWorkflow,
    research_mock_model,
)
from tests.helpers import assert_eq_eventually, new_worker


class MemoryTracingProcessor(TracingProcessor):
    # True for start events, false for end
    trace_events: list[tuple[Trace, bool]] = []
    span_events: list[tuple[Span, bool]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.trace_events.append((trace, True))

    def on_trace_end(self, trace: Trace) -> None:
        self.trace_events.append((trace, False))

    def on_span_start(self, span: Span[Any]) -> None:
        self.span_events.append((span, True))

    def on_span_end(self, span: Span[Any]) -> None:
        self.span_events.append((span, False))

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


@pytest.mark.usefixtures("reset_otel_tracer_provider")
def test_otel_instrumentation_lifecycle_does_not_nest() -> None:
    exporter = set_test_tracer_provider()
    original = OpenInferenceTracingProcessor.on_trace_start
    original_end = OpenInferenceTracingProcessor.on_trace_end
    _temporal_openai_agents._install_otel_instrumentation(
        opentelemetry.trace.get_tracer_provider()
    )
    try:
        installed_patch = OpenInferenceTracingProcessor.on_trace_start
        installed_end = OpenInferenceTracingProcessor.on_trace_end
        assert installed_patch is not original
        assert installed_end is not original_end

        _temporal_openai_agents._install_otel_instrumentation(
            opentelemetry.trace.get_tracer_provider()
        )
        try:
            assert OpenInferenceTracingProcessor.on_trace_start is installed_patch
            assert OpenInferenceTracingProcessor.on_trace_end is installed_end
        finally:
            _temporal_openai_agents._uninstall_otel_instrumentation()

        assert OpenInferenceTracingProcessor.on_trace_start is installed_patch
        assert OpenInferenceTracingProcessor.on_trace_end is installed_end
        previous_context = opentelemetry.context.get_current()
        with pytest.raises(ValueError, match="trace body failed"):
            with trace("Standalone trace"):
                with custom_span("Standalone child"):
                    raise ValueError("trace body failed")
        assert opentelemetry.context.get_current() is previous_context
        processor = openinference_processor()
        assert not processor._root_spans
        assert not processor._otel_spans
        assert not processor._tokens
        assert {span.name for span in exporter.get_finished_spans()} == {
            "Standalone trace",
            "Standalone child",
        }
    finally:
        _temporal_openai_agents._uninstall_otel_instrumentation()

    assert OpenInferenceTracingProcessor.on_trace_start is original
    assert OpenInferenceTracingProcessor.on_trace_end is original_end


async def test_tracing(client: Client):
    async with AgentEnvironment(model=research_mock_model()) as env:
        client = env.applied_on_client(client)
        provider = get_trace_provider()

        processor = MemoryTracingProcessor()
        provider.set_processors([processor])

        async with new_worker(
            client,
            ResearchWorkflow,
        ) as worker:
            with trace("Research workflow"):
                workflow_handle = await client.start_workflow(
                    ResearchWorkflow.run,
                    "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                    id=f"research-workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                await workflow_handle.result()
        print("\n".join([str({"name": t.name}) for t, _ in processor.trace_events]))

        # There are two traces, one is created in the client because it is needed to start the temporal spans
        assert len(processor.trace_events) == 2
        assert (
            processor.trace_events[0][0].trace_id
            == processor.trace_events[1][0].trace_id
        )
        assert processor.trace_events[0][1]
        assert not processor.trace_events[1][1]

        def paired_span(a: tuple[Span[Any], bool], b: tuple[Span[Any], bool]) -> None:
            assert a[0].trace_id == b[0].trace_id
            assert a[1]
            assert not b[1]

        print(
            "\n".join(
                [
                    str({"id": t.span_id, "data": t.span_data.export()})
                    for t, _ in processor.span_events
                ]
            )
        )

        # Start workflow traces
        paired_span(processor.span_events[0], processor.span_events[1])
        assert (
            processor.span_events[0][0].span_data.export().get("name")
            == "temporal:startWorkflow:ResearchWorkflow"
        )

        # Execute workflow
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name")
            == "temporal:executeWorkflow"
        )

        # Research manager span
        paired_span(processor.span_events[3], processor.span_events[-2])
        assert (
            processor.span_events[3][0].span_data.export().get("name")
            == "Research manager"
        )

        # Initial planner spans - task wraps agent, agent wraps turn, turn wraps activity
        paired_span(processor.span_events[4], processor.span_events[13])
        assert processor.span_events[4][0].span_data.export().get("name") == "task"

        paired_span(processor.span_events[5], processor.span_events[12])
        assert (
            processor.span_events[5][0].span_data.export().get("name") == "PlannerAgent"
        )

        paired_span(processor.span_events[6], processor.span_events[11])
        assert processor.span_events[6][0].span_data.export().get("name") == "turn"

        paired_span(processor.span_events[7], processor.span_events[10])
        assert (
            processor.span_events[7][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[8], processor.span_events[9])
        assert (
            processor.span_events[8][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )

        for span, start in processor.span_events[14:-12]:
            span_data = span.span_data.export()

            # All spans should be closed
            if start:
                assert any(
                    span.span_id == s.span_id and not s_start
                    for (s, s_start) in processor.span_events
                )

            # Start activity is always parented to a turn span, which is parented to an agent
            if span_data.get("name") == "temporal:startActivity":
                turn_spans = [
                    s for (s, _) in processor.span_events if s.span_id == span.parent_id
                ]
                assert len(turn_spans) == 2
                assert (
                    turn_spans[0]
                    .span_data.export()
                    .get("data", {})
                    .get("sdk_span_type")
                    == "turn"
                )
                agent_spans = [
                    s
                    for (s, _) in processor.span_events
                    if s.span_id == turn_spans[0].parent_id
                ]
                assert len(agent_spans) == 2
                assert agent_spans[0].span_data.export()["type"] == "agent"

            # Execute is parented to start
            if span_data.get("name") == "temporal:executeActivity":
                parents = [
                    s for (s, _) in processor.span_events if s.span_id == span.parent_id
                ]
                assert (
                    len(parents) == 2
                    and parents[0].span_data.export()["name"]
                    == "temporal:startActivity"
                )

        # Final writer spans - task wraps agent, agent wraps turn, turn wraps activity
        paired_span(processor.span_events[-12], processor.span_events[-3])
        assert processor.span_events[-12][0].span_data.export().get("name") == "task"

        paired_span(processor.span_events[-11], processor.span_events[-4])
        assert (
            processor.span_events[-11][0].span_data.export().get("name")
            == "WriterAgent"
        )

        paired_span(processor.span_events[-10], processor.span_events[-5])
        assert processor.span_events[-10][0].span_data.export().get("name") == "turn"

        paired_span(processor.span_events[-9], processor.span_events[-6])
        assert (
            processor.span_events[-9][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[-8], processor.span_events[-7])
        assert (
            processor.span_events[-8][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )


@activity.defn
async def simple_no_context_activity() -> str:
    return "success"


@workflow.defn
class TraceWorkflow:
    def __init__(self) -> None:
        self._proceed = False
        self._ready = False

    @workflow.run
    async def run(self):
        # Workflow creates spans within existing trace context
        with custom_span("Workflow span"):
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            self._ready = True
            await workflow.wait_condition(lambda: self._proceed)
        return "done"

    @workflow.query
    def ready(self) -> bool:
        return self._ready

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


@workflow.defn
class SelfTracingWorkflow:
    def __init__(self) -> None:
        self._proceed = False
        self._ready = False

    @workflow.run
    async def run(self):
        # Workflow starts its own trace
        with trace("Workflow trace"):
            with custom_span("Workflow span"):
                await workflow.execute_activity(
                    simple_no_context_activity,
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self._ready = True
                await workflow.wait_condition(lambda: self._proceed)
        return "done"

    @workflow.query
    def ready(self) -> bool:
        return self._ready

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


def print_otel_spans(spans: tuple[ReadableSpan, ...]):
    print(
        "\n".join(
            [
                str(
                    {
                        "Name": span.name,
                        "Id": span.context.span_id if span.context else None,
                        "Parent": span.parent.span_id if span.parent else None,
                    }
                )
                for span in spans
            ]
        )
    )


def set_test_tracer_provider() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()

    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)
    # set_tracer_provider is set-once per process: if another test left a
    # global provider installed (e.g. leaked from a sibling test in the same
    # pytest-xdist worker), the call above silently no-ops and every span in
    # this test bypasses the exporter. Fail at the cause instead.
    assert opentelemetry.trace.get_tracer_provider() is provider, (
        "Global tracer provider install was a no-op; a previous test in this"
        " process left a provider set without resetting it"
    )
    return exporter


async def test_external_trace_to_workflow_spans(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test: External trace -> workflow spans (with worker restart)."""
    exporter = set_test_tracer_provider()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow with external trace context
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)
        # Start external trace, then start workflow within that trace
        # Start it outside of the worker to validate provider usage without worker's runcontext
        with env.openai_agents_plugin.tracing_context():
            with trace("External trace"):
                workflow_handle = await new_client.start_workflow(
                    TraceWorkflow.run,
                    id=f"external-trace-workflow-{uuid.uuid4()}",
                    task_queue=task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                workflow_id = workflow_handle.id

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ):
            # Wait for workflow to be ready
            async def ready() -> bool:
                return await workflow_handle.query(TraceWorkflow.ready)

            await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ):
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(TraceWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"

    spans = exporter.get_finished_spans()
    print_otel_spans(spans)

    assert len(spans) >= 2  # External trace + workflow span

    # Find the spans
    external_span = next((s for s in spans if s.name == "External trace"), None)
    workflow_span = next((s for s in spans if s.name == "Workflow span"), None)

    assert external_span is not None, "External trace span should exist"
    assert workflow_span is not None, "Workflow span should exist"

    # Verify parenting: External trace should be root, workflow span should be child of external trace
    assert external_span.parent is None, (
        "External trace should have no parent (be root)"
    )
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert external_span.context is not None, "External span should have context"
    assert workflow_span.parent.span_id == external_span.context.span_id, (
        "Workflow span should be child of external trace"
    )

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(set(span_ids)), (
        f"All spans should have unique IDs, got: {span_ids}"
    )


async def test_external_trace_and_span_to_workflow_spans(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test: External trace + span -> workflow spans (with worker restart)."""
    exporter = set_test_tracer_provider()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow with external trace + span context
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        # Start external trace + span, then start workflow within that context
        # Start it outside of the worker to validate provider usage without worker's runcontext
        with env.openai_agents_plugin.tracing_context():
            with trace("External trace"):
                with custom_span("External span"):
                    workflow_handle = await new_client.start_workflow(
                        TraceWorkflow.run,
                        id=f"external-span-workflow-{uuid.uuid4()}",
                        task_queue=task_queue,
                        execution_timeout=timedelta(seconds=120),
                    )
                    workflow_id = workflow_handle.id

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ):
            # Wait for workflow to be ready
            async def ready() -> bool:
                return await workflow_handle.query(TraceWorkflow.ready)

            await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ):
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(TraceWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"

    spans = exporter.get_finished_spans()

    assert len(spans) >= 3  # External trace + external span + workflow span

    # Find the spans
    external_trace_span = next((s for s in spans if s.name == "External trace"), None)
    external_span = next((s for s in spans if s.name == "External span"), None)
    workflow_span = next((s for s in spans if s.name == "Workflow span"), None)

    assert external_trace_span is not None, "External trace span should exist"
    assert external_span is not None, "External span should exist"
    assert workflow_span is not None, "Workflow span should exist"

    # Verify parenting: External span should be child of trace, workflow span should be child of external span
    assert external_trace_span.parent is None, (
        "External trace should have no parent (be root)"
    )
    assert external_span.parent is not None, "External span should have a parent"
    assert external_trace_span.context is not None, (
        "External trace span should have context"
    )
    assert external_span.parent.span_id == external_trace_span.context.span_id, (
        "External span should be child of external trace"
    )
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert external_span.context is not None, "External span should have context"
    assert workflow_span.parent.span_id == external_span.context.span_id, (
        "Workflow span should be child of external span"
    )

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(set(span_ids)), (
        f"All spans should have unique IDs, got: {span_ids}"
    )


@pytest.mark.parametrize("add_temporal_spans", [False, True])
async def test_workflow_only_trace_to_spans(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
    add_temporal_spans: bool,
):
    """Test: Workflow-only trace -> spans (with worker restart)."""
    exporter = set_test_tracer_provider()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow (no external trace context)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=add_temporal_spans,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            SelfTracingWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            # No external trace - workflow starts its own
            workflow_handle = await new_client.start_workflow(
                SelfTracingWorkflow.run,
                id=f"self-tracing-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            workflow_id = workflow_handle.id

            # Wait for workflow to be ready
            async def ready() -> bool:
                return await workflow_handle.query(SelfTracingWorkflow.ready)

            await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=add_temporal_spans,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            SelfTracingWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(SelfTracingWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"
            processor = openinference_processor()

    spans = exporter.get_finished_spans()
    print_otel_spans(spans)
    assert not processor._root_spans
    assert not processor._otel_spans
    assert not processor._tokens
    span_ids = {span.context.span_id for span in spans if span.context}
    assert len(span_ids) == len(spans)
    assert all(not span.parent or span.parent.span_id in span_ids for span in spans)

    assert len(spans) >= 2  # Workflow trace + workflow span

    # Find the spans
    workflow_trace_span = next((s for s in spans if s.name == "Workflow trace"), None)
    workflow_span = next((s for s in spans if s.name == "Workflow span"), None)

    assert workflow_trace_span is not None, "Workflow trace span should exist"
    assert workflow_span is not None, "Workflow span should exist"

    # Verify parenting: Workflow trace should be root, workflow span should be child of workflow trace
    assert workflow_trace_span.parent is None, (
        "Workflow trace should have no parent (be root)"
    )
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert workflow_trace_span.context is not None, (
        "Workflow trace span should have context"
    )
    assert workflow_span.parent.span_id == workflow_trace_span.context.span_id, (
        "Workflow span should be child of workflow trace"
    )


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        # Use custom_span without starting a trace - should be a no-op
        with custom_span("Should not appear"):
            with custom_span("Neither should this"):
                return "done"


async def test_custom_span_without_trace_context(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test that custom_span() without a trace context emits no spans.

    This validates our hypothesis about why the main test fails:
    If no OpenAI trace is started, custom_span() calls should be no-ops.
    """
    exporter = set_test_tracer_provider()

    async with AgentEnvironment(
        model=research_mock_model(), use_otel_instrumentation=True
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(client, SimpleWorkflow) as worker:
            result = await client.execute_workflow(
                SimpleWorkflow.run,
                id=f"simple-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert result == "done"

    spans = exporter.get_finished_spans()

    # Should have no custom spans since no trace was started
    custom_spans = [
        span
        for span in spans
        if "Should not appear" in span.name or "Neither should this" in span.name
    ]

    assert len(custom_spans) == 0, (
        f"Expected no custom spans without trace context, but found: {[s.name for s in custom_spans]}"
    )

    # Should have no spans at all since no trace was started and spans should be dropped
    assert len(spans) == 0, (
        f"Expected no spans without trace context, but found: {[s.name for s in spans]}"
    )


async def test_otel_tracing_in_runner(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test the tracing when executing an actual OpenAI Runner."""
    exporter = set_test_tracer_provider()

    # Test the new ergonomic API - just pass exporters to AgentEnvironment
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            ResearchWorkflow,
            max_cached_workflows=0,
        ) as worker:
            with env.openai_agents_plugin.tracing_context(), trace("Research workflow"):
                workflow_handle = await client.start_workflow(
                    ResearchWorkflow.run,
                    "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                    id=f"research-workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                await workflow_handle.result()

    spans = exporter.get_finished_spans()
    print("OTEL tracing in runner spans:")
    print_otel_spans(spans)

    # Verify basic span capture
    assert len(spans) > 0, "Should have captured some spans from the research workflow"

    # Categorize spans that users expect to see in their agents workflow
    research_manager_spans = [span for span in spans if "Research manager" in span.name]
    search_web_spans = [span for span in spans if "Search the web" in span.name]
    agent_execution_spans = [
        span
        for span in spans
        if any(
            agent_name in span.name.lower()
            for agent_name in ["planner", "search", "writer"]
        )
        and "workflow" not in span.name.lower()
    ]

    all_span_names = [span.name for span in spans]
    unique_span_names = list(set(all_span_names))

    # Assert users get visibility into their workflow coordination
    assert len(research_manager_spans) > 0, (
        f"Expected 'Research manager' spans for workflow coordination visibility, "
        f"but only found: {unique_span_names}"
    )

    # Assert users can see their search phases
    assert len(search_web_spans) > 0, (
        f"Expected 'Search the web' spans for search phase visibility, "
        f"but only found: {unique_span_names}"
    )

    # Assert users can see individual agent executions
    assert len(agent_execution_spans) > 0, (
        f"Expected agent execution spans (planner, search, writer) for individual agent visibility, "
        f"but only found: {unique_span_names}"
    )

    # Validate span hierarchy integrity
    span_ids = {span.context.span_id for span in spans if span.context}
    for span in spans:
        if span.parent:
            assert span.parent.span_id in span_ids, (
                f"Span '{span.name}' has invalid parent reference - parent span doesn't exist"
            )

    # Validate logical parent-child relationships match user code structure
    workflow_trace_spans = [span for span in spans if "Research workflow" in span.name]
    assert len(workflow_trace_spans) == 1, (
        f"Expected exactly one 'Research workflow' trace, got {len(workflow_trace_spans)}"
    )
    workflow_span = workflow_trace_spans[0]
    assert workflow_span.context is not None

    # Research manager should be child of workflow trace
    research_span = research_manager_spans[0]
    assert research_span.context is not None
    assert research_span.parent is not None, (
        "Research manager span should have a parent"
    )
    assert research_span.parent.span_id == workflow_span.context.span_id, (
        "Expected 'Research manager' to be child of 'Research workflow' trace"
    )

    # Search the web should be child of research manager
    search_span = search_web_spans[0]
    assert search_span.context is not None
    assert search_span.parent is not None, "Search the web span should have a parent"
    assert search_span.parent.span_id == research_span.context.span_id, (
        "Expected 'Search the web' to be child of 'Research manager' span"
    )

    # All search agent spans should be descendants of "Search the web"
    # (the SDK now inserts a "task" span between "Search the web" and the agent)
    span_by_id = {span.context.span_id: span for span in spans if span.context}
    search_agent_spans = [span for span in spans if "Search agent" in span.name]

    def is_descendant_of(child: ReadableSpan, ancestor_span_id: int) -> bool:
        """Check if child is a descendant of the span with ancestor_span_id."""
        current: ReadableSpan | None = child
        while current and current.parent:
            if current.parent.span_id == ancestor_span_id:
                return True
            current = span_by_id.get(current.parent.span_id)
        return False

    for search_agent_span in search_agent_spans:
        assert search_agent_span.parent is not None, (
            f"Search agent span '{search_agent_span.name}' should have a parent"
        )
        assert is_descendant_of(search_agent_span, search_span.context.span_id), (
            f"Expected all 'Search agent' spans to be descendants of 'Search the web' span"
        )

    # PlannerAgent and WriterAgent should be descendants of research manager
    planner_spans = [span for span in spans if "PlannerAgent" in span.name]
    writer_spans = [span for span in spans if "WriterAgent" in span.name]

    for planner_span in planner_spans:
        assert planner_span.parent is not None, "PlannerAgent span should have a parent"
        assert is_descendant_of(planner_span, research_span.context.span_id), (
            "Expected 'PlannerAgent' to be descendant of 'Research manager' span"
        )

    for writer_span in writer_spans:
        assert writer_span.parent is not None, "WriterAgent span should have a parent"
        assert is_descendant_of(writer_span, research_span.context.span_id), (
            "Expected 'WriterAgent' to be descendant of 'Research manager' span"
        )


@workflow.defn
class OtelSpanWorkflow:
    def __init__(self) -> None:
        self._proceed = False
        self._ready = False

    @workflow.run
    async def run(self):
        # Start an SDK custom_span first to establish OTEL context
        with custom_span("Workflow SDK span"):
            # Workflow starts OTEL span directly using opentelemetry.trace
            tracer = opentelemetry.trace.get_tracer(__name__)
            with tracer.start_as_current_span("Direct OTEL span"):
                await workflow.execute_activity(
                    simple_no_context_activity,
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self._ready = True
                await workflow.wait_condition(lambda: self._proceed)
        return "done"

    @workflow.query
    def ready(self) -> bool:
        return self._ready

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


async def test_sdk_trace_to_otel_span_parenting(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test that OTEL spans started in workflow are properly parented to client SDK trace."""
    exporter = set_test_tracer_provider()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow with client SDK trace context
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            OtelSpanWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
            workflow_runner=SandboxedWorkflowRunner(
                SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
            ),
        ) as worker:
            # Start SDK trace in client, then start workflow within that trace
            with env.openai_agents_plugin.tracing_context(), trace("Client SDK trace"):
                workflow_handle = await new_client.start_workflow(
                    OtelSpanWorkflow.run,
                    id=f"sdk-trace-otel-span-workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                workflow_id = workflow_handle.id

                # Wait for workflow to be ready
                async def ready() -> bool:
                    return await workflow_handle.query(OtelSpanWorkflow.ready)

                await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
        use_otel_instrumentation=True,
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            OtelSpanWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
            workflow_runner=SandboxedWorkflowRunner(
                SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
            ),
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(OtelSpanWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"

    spans = exporter.get_finished_spans()
    print("SDK trace to OTEL span parenting:")
    print_otel_spans(spans)

    assert len(spans) >= 3  # Client SDK trace + Workflow SDK span + Direct OTEL span

    # Find the spans
    client_sdk_trace_span = next(
        (s for s in spans if s.name == "Client SDK trace"), None
    )
    workflow_sdk_span = next((s for s in spans if s.name == "Workflow SDK span"), None)
    direct_otel_span = next((s for s in spans if s.name == "Direct OTEL span"), None)

    assert client_sdk_trace_span is not None, "Client SDK trace span should exist"
    assert workflow_sdk_span is not None, "Workflow SDK span should exist"
    assert direct_otel_span is not None, "Direct OTEL span should exist"

    # Verify parenting chain: Client SDK trace -> Workflow SDK span -> Direct OTEL span
    assert client_sdk_trace_span.parent is None, (
        "Client SDK trace should have no parent (be root)"
    )

    assert workflow_sdk_span.parent is not None, (
        "Workflow SDK span should have a parent"
    )
    assert client_sdk_trace_span.context is not None, (
        "Client SDK trace span should have context"
    )
    assert workflow_sdk_span.parent.span_id == client_sdk_trace_span.context.span_id, (
        "Workflow SDK span should be child of Client SDK trace"
    )

    assert direct_otel_span.parent is not None, "Direct OTEL span should have a parent"
    assert workflow_sdk_span.context is not None, (
        "Workflow SDK span should have context"
    )
    assert direct_otel_span.parent.span_id == workflow_sdk_span.context.span_id, (
        "Direct OTEL span should be child of Workflow SDK span"
    )

    # Verify all spans belong to the same trace
    assert workflow_sdk_span.context is not None, (
        "Workflow SDK span should have context"
    )
    assert direct_otel_span.context is not None, "Direct OTEL span should have context"
    assert (
        client_sdk_trace_span.context.trace_id
        == workflow_sdk_span.context.trace_id
        == direct_otel_span.context.trace_id
    ), "All spans should belong to the same trace"

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(set(span_ids)), (
        f"All spans should have unique IDs, got: {span_ids}"
    )


def openinference_processor() -> OpenInferenceTracingProcessor:
    provider = get_trace_provider()
    if isinstance(provider, TemporalTraceProvider):
        provider = provider._original_provider
    assert isinstance(provider, DefaultTraceProvider)
    return next(
        processor
        for processor in provider._multi_processor._processors
        if isinstance(processor, OpenInferenceTracingProcessor)
    )


@function_tool
async def trace_test_tool() -> str:
    return "tool result"


@workflow.defn
class AgentToolTraceWorkflow:
    @workflow.run
    async def run(self) -> str:
        result = await Runner.run(
            starting_agent=Agent(name="Trace agent", tools=[trace_test_tool]),
            input="Call the tool.",
        )
        return str(result.final_output)


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
@pytest.mark.usefixtures("reset_otel_tracer_provider")
async def test_otel_remote_context_preserves_root(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    max_cached_workflows: int,
) -> None:
    exporter = set_test_tracer_provider()
    roots: list[tuple[int, int, str]] = []
    retained: list[tuple[int, int, int]] = []
    restored: list[bool] = []
    async with AgentEnvironment(
        model=TestModel.returning_responses(
            [
                response
                for _ in range(3)
                for response in (
                    ResponseBuilders.tool_call(arguments="{}", name="trace_test_tool"),
                    ResponseBuilders.output_message("done"),
                )
            ]
        ),
        use_otel_instrumentation=True,
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            AgentToolTraceWorkflow,
            max_cached_workflows=max_cached_workflows,
        ) as worker:
            with opentelemetry.trace.get_tracer(__name__).start_as_current_span(
                "Outer span"
            ):
                parent_context = opentelemetry.context.get_current()
                for index in range(3):
                    with env.openai_agents_plugin.tracing_context():
                        processor = openinference_processor()
                        with trace(f"Client trace {index}"):
                            root = opentelemetry.trace.get_current_span()
                            session_id = f"session-{index}"
                            root.set_attribute(SpanAttributes.SESSION_ID, session_id)
                            context = root.get_span_context()
                            roots.append(
                                (context.trace_id, context.span_id, session_id)
                            )
                            assert (
                                await client.execute_workflow(
                                    AgentToolTraceWorkflow.run,
                                    id=str(uuid.uuid4()),
                                    task_queue=worker.task_queue,
                                )
                                == "done"
                            )
                        restored.append(
                            opentelemetry.context.get_current() is parent_context
                        )
                        retained.append(
                            (
                                len(processor._root_spans),
                                len(processor._otel_spans),
                                len(processor._tokens),
                            )
                        )

    spans = exporter.get_finished_spans()
    spans_by_id = {span.context.span_id: span for span in spans if span.context}
    missing_parents = [
        span.name
        for span in spans
        if span.parent and span.parent.span_id not in spans_by_id
    ]
    print_otel_spans(spans)
    print(f"Client roots: {roots}")
    print(f"Missing parents: {missing_parents}")
    print(f"Retained roots/spans/tokens: {retained}")
    print(f"Caller context restored: {restored}")
    for trace_id, span_id, session_id in roots:
        assert span_id in spans_by_id, "The original client root was not exported"
        root_span = spans_by_id[span_id]
        assert root_span.context and root_span.context.trace_id == trace_id
        assert root_span.attributes
        assert root_span.attributes[SpanAttributes.SESSION_ID] == session_id
    assert len(spans_by_id) == len(spans)
    assert not missing_parents
    assert retained == [(0, 0, 0)] * 3
    assert all(restored)
    for span in spans:
        if span.name == "trace_test_tool":
            assert span.parent
            assert spans_by_id[span.parent.span_id].name == "turn"
    assert "Failed to detach context" not in caplog.text


@workflow.defn
class CallbackChildWorkflow:
    @workflow.run
    async def run(self, outcome: str = "success") -> str:
        if outcome == "failure":
            raise ApplicationError("Child failed", non_retryable=True)
        if outcome == "cancel":
            await workflow.wait_condition(lambda: False)
        return "success"


@activity.defn
async def callback_outcome_activity(outcome: str) -> str:
    if outcome == "failure":
        raise ApplicationError("Activity failed", non_retryable=True)
    if outcome == "cancel":
        await asyncio.Future()
    return "success"


@workflow.defn
class CallbackSpanWorkflow:
    @workflow.run
    async def run(self, outcome: str = "success") -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        errors: list[str] = []
        with custom_span("Callback caller") as caller:
            otel_caller = opentelemetry.trace.get_current_span().get_span_context()
            for operation in ("activity", "local_activity", "child_workflow"):
                handle: asyncio.Future[Any]
                if operation == "activity" and outcome == "success":
                    handle = workflow.start_activity(
                        simple_no_context_activity,
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                elif operation == "local_activity" and outcome == "success":
                    handle = workflow.start_local_activity(
                        simple_no_context_activity,
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                elif operation == "child_workflow":
                    handle = await workflow.start_child_workflow(
                        CallbackChildWorkflow.run,
                        arg=outcome,
                    )
                else:
                    start = (
                        workflow.start_activity
                        if operation == "activity"
                        else workflow.start_local_activity
                    )
                    handle = start(
                        callback_outcome_activity,
                        arg=outcome,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                for phase in ("scheduled", "completed"):
                    if phase == "completed":
                        if outcome == "cancel":
                            handle.cancel()
                        try:
                            await handle
                        except (
                            ActivityError,
                            ApplicationError,
                            ChildWorkflowError,
                            CancelledError,
                            asyncio.CancelledError,
                        ):
                            errors.append(operation)
                    current = get_trace_provider().get_current_span()
                    observations.append(
                        {
                            "operation": operation,
                            "phase": phase,
                            "agent_span": current.span_id if current else None,
                            "otel_span": opentelemetry.trace.get_current_span()
                            .get_span_context()
                            .span_id,
                        }
                    )
                with custom_span(f"After {operation}"):
                    pass
        return {
            "agent_span": caller.span_id,
            "otel_span": otel_caller.span_id,
            "observations": observations,
            "errors": errors,
        }


@pytest.mark.usefixtures("reset_otel_tracer_provider")
async def test_otel_callback_spans_restore_context(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    max_cached_workflows: int = 1000,
) -> None:
    exporter = set_test_tracer_provider()
    async with AgentEnvironment(
        model=research_mock_model(),
        use_otel_instrumentation=True,
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            CallbackSpanWorkflow,
            CallbackChildWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=max_cached_workflows,
            workflow_runner=SandboxedWorkflowRunner(
                SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
            ),
        ) as worker:
            with env.openai_agents_plugin.tracing_context():
                processor = openinference_processor()
                with trace("Callback trace"):
                    result = await client.execute_workflow(
                        CallbackSpanWorkflow.run,
                        id=str(uuid.uuid4()),
                        task_queue=worker.task_queue,
                    )
    spans = exporter.get_finished_spans()
    print_otel_spans(spans)
    print(f"Callback contexts: {result}")
    print(
        "Retained roots/spans/tokens:",
        len(processor._root_spans),
        len(processor._otel_spans),
        len(processor._tokens),
    )
    assert "Failed to detach context" not in caplog.text
    for observation in result["observations"]:
        assert observation["agent_span"] == result["agent_span"]
        assert observation["otel_span"] == result["otel_span"]
    assert not processor._root_spans
    assert not processor._otel_spans
    assert not processor._tokens
    assert len(spans) == len({span.context.span_id for span in spans if span.context})
    for span in spans:
        if span.name.startswith("After "):
            assert span.parent and span.parent.span_id == result["otel_span"]


@pytest.mark.usefixtures("reset_otel_tracer_provider")
async def test_otel_callback_replay(
    client: Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await test_otel_callback_spans_restore_context(
        client=client,
        caplog=caplog,
        max_cached_workflows=0,
    )


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
@pytest.mark.usefixtures("reset_otel_tracer_provider")
async def test_otel_callback_failure_and_cancellation(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    exporter = set_test_tracer_provider()
    async with AgentEnvironment(
        model=research_mock_model(),
        use_otel_instrumentation=True,
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            CallbackSpanWorkflow,
            CallbackChildWorkflow,
            activities=[simple_no_context_activity, callback_outcome_activity],
            workflow_runner=SandboxedWorkflowRunner(
                SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
            ),
        ) as worker:
            with env.openai_agents_plugin.tracing_context():
                processor = openinference_processor()
                with trace("Callback outcome trace"):
                    result = await client.execute_workflow(
                        CallbackSpanWorkflow.run,
                        arg=outcome,
                        id=str(uuid.uuid4()),
                        task_queue=worker.task_queue,
                    )
    print(f"Callback outcome {outcome}: {result}")
    print_otel_spans(exporter.get_finished_spans())
    assert result["errors"] == ["activity", "local_activity", "child_workflow"]
    for observation in result["observations"]:
        assert observation["agent_span"] == result["agent_span"]
        assert observation["otel_span"] == result["otel_span"]
    assert not processor._root_spans
    assert not processor._otel_spans
    assert not processor._tokens
    assert "Failed to detach context" not in caplog.text


@activity.defn
async def inspect_otel_context_activity() -> tuple[int, int, str]:
    context = opentelemetry.trace.get_current_span().get_span_context()
    return context.trace_id, int(context.trace_flags), context.trace_state.to_header()


@workflow.defn
class InspectOtelContextWorkflow:
    @workflow.run
    async def run(self) -> tuple[int, int, str]:
        return await workflow.execute_activity(
            inspect_otel_context_activity,
            start_to_close_timeout=timedelta(seconds=30),
        )


@pytest.mark.parametrize("sampled", [True, False])
@pytest.mark.usefixtures("reset_otel_tracer_provider")
async def test_otel_remote_sampling_context(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    sampled: bool,
) -> None:
    exporter = set_test_tracer_provider()
    id_generator = RandomIdGenerator()
    remote_context = opentelemetry.trace.SpanContext(
        trace_id=id_generator.generate_trace_id(),
        span_id=id_generator.generate_span_id(),
        is_remote=True,
        trace_flags=opentelemetry.trace.TraceFlags(
            opentelemetry.trace.TraceFlags.SAMPLED
            if sampled
            else opentelemetry.trace.TraceFlags.DEFAULT
        ),
        trace_state=opentelemetry.trace.TraceState([("vendor", "state")]),
    )
    async with AgentEnvironment(
        model=research_mock_model(),
        use_otel_instrumentation=True,
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            InspectOtelContextWorkflow,
            activities=[inspect_otel_context_activity],
        ) as worker:
            with env.openai_agents_plugin.tracing_context():
                processor = openinference_processor()
                with opentelemetry.trace.use_span(
                    opentelemetry.trace.NonRecordingSpan(remote_context)
                ):
                    with trace("Remote sampling"):
                        result = await client.execute_workflow(
                            InspectOtelContextWorkflow.run,
                            id=str(uuid.uuid4()),
                            task_queue=worker.task_queue,
                        )
    print(f"Remote sampling {sampled}: {result}")
    assert result == (
        remote_context.trace_id,
        int(remote_context.trace_flags),
        remote_context.trace_state.to_header(),
    )
    assert bool(exporter.get_finished_spans()) == sampled
    assert not processor._root_spans
    assert not processor._otel_spans
    assert not processor._tokens
    assert "Failed to detach context" not in caplog.text


@pytest.mark.parametrize("add_temporal_spans", [True, False])
async def test_callback_context_without_otel(
    client: Client, add_temporal_spans: bool
) -> None:
    processor = MemoryTracingProcessor()
    processor.trace_events = []
    processor.span_events = []
    get_trace_provider().set_processors([processor])
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=add_temporal_spans,
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client,
            CallbackSpanWorkflow,
            CallbackChildWorkflow,
            activities=[simple_no_context_activity],
            workflow_runner=SandboxedWorkflowRunner(
                SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
            ),
        ) as worker:
            with trace("Native callback trace"):
                result = await client.execute_workflow(
                    CallbackSpanWorkflow.run,
                    id=str(uuid.uuid4()),
                    task_queue=worker.task_queue,
                )
    print(f"Native tracing, temporal spans {add_temporal_spans}: {result}")
    assert result["agent_span"] != "no-op"
    for observation in result["observations"]:
        assert observation["agent_span"] == result["agent_span"]
    started = {span.span_id for span, start in processor.span_events if start}
    ended = {span.span_id for span, start in processor.span_events if not start}
    assert started == ended
    assert (
        any(
            span.span_data.export().get("name") == "temporal:startActivity"
            for span, _ in processor.span_events
        )
        == add_temporal_spans
    )
