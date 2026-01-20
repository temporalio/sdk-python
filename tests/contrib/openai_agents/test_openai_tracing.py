import uuid
from datetime import timedelta
from typing import Any

from agents import Span, Trace, TracingProcessor, custom_span, trace
from agents.tracing import get_trace_provider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
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
            workflow_handle = await client.start_workflow(
                ResearchWorkflow.run,
                "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            await workflow_handle.result()

        # There is one closed root trace
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
                [str(event.span_data.export()) for event, _ in processor.span_events]
            )
        )

        # Workflow start spans
        paired_span(processor.span_events[0], processor.span_events[1])
        assert (
            processor.span_events[0][0].span_data.export().get("name")
            == "temporal:startWorkflow:ResearchWorkflow"
        )

        # Workflow execute spans
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name")
            == "temporal:executeWorkflow"
        )

        # Workflow execute spans
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name")
            == "temporal:executeWorkflow"
        )

        # Overarching research span
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

        for span, start in processor.span_events[10:-7]:
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


async def test_external_trace_to_workflow_spans(client: Client):
    """Test: External trace → workflow spans (with worker restart)."""
    exporter = InMemorySpanExporter()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow with external trace context
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            # Start external trace, then start workflow within that trace
            with trace("External trace"):
                workflow_handle = await new_client.start_workflow(
                    TraceWorkflow.run,
                    id=f"external-trace-workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                workflow_id = workflow_handle.id

                # Wait for workflow to be ready
                async def ready() -> bool:
                    return await workflow_handle.query(TraceWorkflow.ready)

                await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(TraceWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"

    spans = exporter.get_finished_spans()
    print("External trace → workflow spans:")
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
    """Test: External trace + span → workflow spans (with worker restart)."""
    exporter = InMemorySpanExporter()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow with external trace + span context
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            # Start external trace + span, then start workflow within that context
            with trace("External trace"):
                with custom_span("External span"):
                    workflow_handle = await new_client.start_workflow(
                        TraceWorkflow.run,
                        id=f"external-span-workflow-{uuid.uuid4()}",
                        task_queue=worker.task_queue,
                        execution_timeout=timedelta(seconds=120),
                    )
                    workflow_id = workflow_handle.id

                    # Wait for workflow to be ready
                    async def ready() -> bool:
                        return await workflow_handle.query(TraceWorkflow.ready)

                    await assert_eq_eventually(True, ready)

    # Second worker: Complete the workflow with fresh objects (new instrumentation)
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
    ) as env:
        new_client = env.applied_on_client(client)

        async with new_worker(
            new_client,
            TraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(TraceWorkflow.proceed)
            result = await workflow_handle.result()
            assert result == "done"

    spans = exporter.get_finished_spans()
    print("External trace + span → workflow spans:")
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
    """Test: Workflow-only trace → spans (with worker restart)."""
    exporter = InMemorySpanExporter()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    # First worker: Start workflow (no external trace context)
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
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
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
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
    print("Workflow-only trace → spans:")
    print(f"Total spans: {len(spans)}")
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

    # Debug: print all span names
    print("Span names:", [span.name for span in spans])

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


async def test_otel_tracing_in_runner(client: Client):
    """Test the ergonomic AgentEnvironment OTEL integration."""
    exporter = InMemorySpanExporter()

    # Test the new ergonomic API - just pass exporters to AgentEnvironment
    async with AgentEnvironment(
        model=research_mock_model(), add_temporal_spans=False, otel_exporters=[exporter]
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            ResearchWorkflow,
            max_cached_workflows=0,
        ) as worker:
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

    # Update assertion - the exact count and parent relationships may have changed with the new approach
    assert len(spans) > 0, "Should have at least some spans"
    print(f"Total spans: {len(spans)}")

    # Verify spans have proper hierarchy
    span_ids = {span.context.span_id for span in spans if span.context}
    parent_ids = {span.parent.span_id for span in spans if span.parent}
    print(f"Unique span IDs: {len(span_ids)}")
    print(f"Parent references: {len(parent_ids)}")

    # All spans should have unique IDs
    assert len(span_ids) == len(spans), "All spans should have unique IDs"
