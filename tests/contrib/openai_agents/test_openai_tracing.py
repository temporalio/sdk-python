import uuid
from datetime import timedelta
from typing import Any

import opentelemetry.trace
from agents import Span, Trace, TracingProcessor, custom_span, trace
from agents.tracing import get_trace_provider
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
)
from temporalio.contrib.opentelemetry import create_tracer_provider
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

        # Initial planner spans - There are only 3 because we don't make an actual model call
        paired_span(processor.span_events[4], processor.span_events[9])
        assert (
            processor.span_events[4][0].span_data.export().get("name") == "PlannerAgent"
        )

        paired_span(processor.span_events[5], processor.span_events[8])
        assert (
            processor.span_events[5][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[6], processor.span_events[7])
        assert (
            processor.span_events[6][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )

        for span, start in processor.span_events[10:-8]:
            span_data = span.span_data.export()

            # All spans should be closed
            if start:
                assert any(
                    span.span_id == s.span_id and not s_start
                    for (s, s_start) in processor.span_events
                )

            # Start activity is always parented to an agent
            if span_data.get("name") == "temporal:startActivity":
                parents = [
                    s for (s, _) in processor.span_events if s.span_id == span.parent_id
                ]
                assert (
                    len(parents) == 2
                    and parents[0].span_data.export()["type"] == "agent"
                )

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

        # Final writer spans - There are only 3 because we don't make an actual model call
        paired_span(processor.span_events[-8], processor.span_events[-3])
        assert (
            processor.span_events[-8][0].span_data.export().get("name") == "WriterAgent"
        )

        paired_span(processor.span_events[-7], processor.span_events[-4])
        assert (
            processor.span_events[-7][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[-6], processor.span_events[-5])
        assert (
            processor.span_events[-6][0].span_data.export().get("name")
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

    # Reset global so tests don't conflict
    from opentelemetry.util._once import Once

    opentelemetry.trace._TRACER_PROVIDER_SET_ONCE = Once()
    opentelemetry.trace._TRACER_PROVIDER = None

    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)
    return exporter


async def test_external_trace_to_workflow_spans(client: Client):
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
    assert (
        external_span.parent is None
    ), "External trace should have no parent (be root)"
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert external_span.context is not None, "External span should have context"
    assert (
        workflow_span.parent.span_id == external_span.context.span_id
    ), "Workflow span should be child of external trace"

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(
        set(span_ids)
    ), f"All spans should have unique IDs, got: {span_ids}"


async def test_external_trace_and_span_to_workflow_spans(client: Client):
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
    assert (
        external_trace_span.parent is None
    ), "External trace should have no parent (be root)"
    assert external_span.parent is not None, "External span should have a parent"
    assert (
        external_trace_span.context is not None
    ), "External trace span should have context"
    assert (
        external_span.parent.span_id == external_trace_span.context.span_id
    ), "External span should be child of external trace"
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert external_span.context is not None, "External span should have context"
    assert (
        workflow_span.parent.span_id == external_span.context.span_id
    ), "Workflow span should be child of external span"

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(
        set(span_ids)
    ), f"All spans should have unique IDs, got: {span_ids}"


async def test_workflow_only_trace_to_spans(client: Client):
    """Test: Workflow-only trace -> spans (with worker restart)."""
    exporter = set_test_tracer_provider()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow (no external trace context)
    async with AgentEnvironment(
        model=research_mock_model(),
        add_temporal_spans=False,
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
        add_temporal_spans=False,
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

    spans = exporter.get_finished_spans()

    assert len(spans) >= 2  # Workflow trace + workflow span

    # Find the spans
    workflow_trace_span = next((s for s in spans if s.name == "Workflow trace"), None)
    workflow_span = next((s for s in spans if s.name == "Workflow span"), None)

    assert workflow_trace_span is not None, "Workflow trace span should exist"
    assert workflow_span is not None, "Workflow span should exist"

    # Verify parenting: Workflow trace should be root, workflow span should be child of workflow trace
    assert (
        workflow_trace_span.parent is None
    ), "Workflow trace should have no parent (be root)"
    assert workflow_span.parent is not None, "Workflow span should have a parent"
    assert (
        workflow_trace_span.context is not None
    ), "Workflow trace span should have context"
    assert (
        workflow_span.parent.span_id == workflow_trace_span.context.span_id
    ), "Workflow span should be child of workflow trace"


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        # Use custom_span without starting a trace - should be a no-op
        with custom_span("Should not appear"):
            with custom_span("Neither should this"):
                return "done"


async def test_custom_span_without_trace_context(client: Client):
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

    assert (
        len(custom_spans) == 0
    ), f"Expected no custom spans without trace context, but found: {[s.name for s in custom_spans]}"

    # Should have no spans at all since no trace was started and spans should be dropped
    assert (
        len(spans) == 0
    ), f"Expected no spans without trace context, but found: {[s.name for s in spans]}"


async def test_otel_tracing_in_runner(client: Client):
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
            with trace("Research workflow"):
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
            assert (
                span.parent.span_id in span_ids
            ), f"Span '{span.name}' has invalid parent reference - parent span doesn't exist"

    # Validate logical parent-child relationships match user code structure
    workflow_trace_spans = [span for span in spans if "Research workflow" in span.name]
    assert (
        len(workflow_trace_spans) == 1
    ), f"Expected exactly one 'Research workflow' trace, got {len(workflow_trace_spans)}"
    workflow_span = workflow_trace_spans[0]
    assert workflow_span.context is not None

    # Research manager should be child of workflow trace
    research_span = research_manager_spans[0]
    assert research_span.context is not None
    assert (
        research_span.parent is not None
    ), "Research manager span should have a parent"
    assert (
        research_span.parent.span_id == workflow_span.context.span_id
    ), "Expected 'Research manager' to be child of 'Research workflow' trace"

    # Search the web should be child of research manager
    search_span = search_web_spans[0]
    assert search_span.context is not None
    assert search_span.parent is not None, "Search the web span should have a parent"
    assert (
        search_span.parent.span_id == research_span.context.span_id
    ), "Expected 'Search the web' to be child of 'Research manager' span"

    # All search agent spans should be children of "Search the web"
    search_agent_spans = [span for span in spans if "Search agent" in span.name]
    for search_agent_span in search_agent_spans:
        assert (
            search_agent_span.parent is not None
        ), f"Search agent span '{search_agent_span.name}' should have a parent"
        assert (
            search_agent_span.parent.span_id == search_span.context.span_id
        ), f"Expected all 'Search agent' spans to be children of 'Search the web' span"

    # PlannerAgent and WriterAgent should be children of research manager
    planner_spans = [span for span in spans if "PlannerAgent" in span.name]
    writer_spans = [span for span in spans if "WriterAgent" in span.name]

    for planner_span in planner_spans:
        assert planner_span.parent is not None, "PlannerAgent span should have a parent"
        assert (
            planner_span.parent.span_id == research_span.context.span_id
        ), "Expected 'PlannerAgent' to be child of 'Research manager' span"

    for writer_span in writer_spans:
        assert writer_span.parent is not None, "WriterAgent span should have a parent"
        assert (
            writer_span.parent.span_id == research_span.context.span_id
        ), "Expected 'WriterAgent' to be child of 'Research manager' span"


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


async def test_sdk_trace_to_otel_span_parenting(client: Client):
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
            with trace("Client SDK trace"):
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
    assert (
        client_sdk_trace_span.parent is None
    ), "Client SDK trace should have no parent (be root)"

    assert (
        workflow_sdk_span.parent is not None
    ), "Workflow SDK span should have a parent"
    assert (
        client_sdk_trace_span.context is not None
    ), "Client SDK trace span should have context"
    assert (
        workflow_sdk_span.parent.span_id == client_sdk_trace_span.context.span_id
    ), "Workflow SDK span should be child of Client SDK trace"

    assert direct_otel_span.parent is not None, "Direct OTEL span should have a parent"
    assert (
        workflow_sdk_span.context is not None
    ), "Workflow SDK span should have context"
    assert (
        direct_otel_span.parent.span_id == workflow_sdk_span.context.span_id
    ), "Direct OTEL span should be child of Workflow SDK span"

    # Verify all spans belong to the same trace
    assert (
        workflow_sdk_span.context is not None
    ), "Workflow SDK span should have context"
    assert direct_otel_span.context is not None, "Direct OTEL span should have context"
    assert (
        client_sdk_trace_span.context.trace_id
        == workflow_sdk_span.context.trace_id
        == direct_otel_span.context.trace_id
    ), "All spans should belong to the same trace"

    # Verify all spans have unique IDs
    span_ids = [span.context.span_id for span in spans if span.context]
    assert len(span_ids) == len(
        set(span_ids)
    ), f"All spans should have unique IDs, got: {span_ids}"
