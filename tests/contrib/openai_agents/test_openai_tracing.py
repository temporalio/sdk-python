import traceback
import uuid
from datetime import timedelta
from typing import Any

import opentelemetry.trace
from agents import Span, Trace, TracingProcessor, trace, custom_span
from agents.tracing import get_trace_provider
from opentelemetry.sdk.trace import SpanProcessor, ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import INVALID_TRACE_ID, INVALID_SPAN_ID, get_current_span

import temporalio.contrib.opentelemetryv2
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
)
from temporalio.contrib.openai_agents._temporal_trace_provider import TemporalIdGenerator
from tests.contrib.openai_agents.test_openai import (
    ResearchWorkflow,
    research_mock_model,
)
from tests.helpers import new_worker, assert_eq_eventually
from opentelemetry.sdk import trace as trace_sdk
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

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

        print("\n".join([str(event.span_data.export()) for event, _ in processor.span_events]))

        # Workflow start spans
        paired_span(processor.span_events[0], processor.span_events[1])
        assert (
            processor.span_events[0][0].span_data.export().get("name") == "temporal:startWorkflow:ResearchWorkflow"
        )

        # Workflow execute spans
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name") == "temporal:executeWorkflow"
        )

        # Workflow execute spans
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name") == "temporal:executeWorkflow"
        )

        # Overarching research span
        paired_span(processor.span_events[3], processor.span_events[-2])
        assert (
            processor.span_events[3][0].span_data.export().get("name") == "Research manager"
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
class BasicTraceWorkflow:
    def __init__(self) -> None:
        self.proceed = False
        self.ready = False

    @workflow.run
    async def run(self):
        with custom_span("Research manager"):
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            self.ready = True
            await workflow.wait_condition(lambda: self.proceed)

            with custom_span("Inner"):
                await workflow.execute_activity(
                    simple_no_context_activity,
                    start_to_close_timeout=timedelta(seconds=10),
                )
                return

    @workflow.signal
    def proceed(self) -> None:
        self.proceed = True

    @workflow.query
    def ready(self) -> bool:
        return self.ready


async def test_otel_tracing_parent_trace(client: Client):
    exporter = InMemorySpanExporter()
    workflow_id = None
    task_queue = str(uuid.uuid4())

    async with AgentEnvironment(model=research_mock_model(), add_temporal_spans=False, use_otel=True) as env:
        new_client = env.applied_on_client(client)

        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

        new_config = new_client.config()
        new_config["interceptors"] = list(new_config["interceptors"]) + [
            temporalio.contrib.opentelemetryv2.TracingInterceptor(tracer=provider.get_tracer(__name__))]
        new_client = Client(**new_config)

        async with new_worker(
            new_client,
            BasicTraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            with trace("Research workflow"):
                workflow_handle = await new_client.start_workflow(
                    BasicTraceWorkflow.run,
                    id=f"research-workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                workflow_id = workflow_handle.id
                async def ready() -> bool:
                    return await workflow_handle.query(BasicTraceWorkflow.ready)

                await assert_eq_eventually(True, ready)

    # Restart the worker with all new objects
    async with AgentEnvironment(model=research_mock_model(), add_temporal_spans=False,
                                use_otel=True) as env:
        new_client = env.applied_on_client(client)

        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

        async with new_worker(
            new_client,
            BasicTraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(BasicTraceWorkflow.proceed)
            await workflow_handle.result()

    spans = exporter.get_finished_spans()
    print("\n".join(
        [str({"Name": span.name, "Id": span.context.span_id, "Parent": span.parent.span_id if span.parent else None})
         for span in spans]))
    assert len(spans) == 3
    assert spans[0].parent == None
    assert spans[1].parent.span_id == spans[2].context.span_id
    assert spans[2].parent.span_id == spans[0].context.span_id


async def test_otel_tracing_parent_span(client: Client):
    exporter = InMemorySpanExporter()
    workflow_id = None
    task_queue = str(uuid.uuid4())


    async with AgentEnvironment(model=research_mock_model(), add_temporal_spans=False, use_otel=True) as env:
        new_client = env.applied_on_client(client)

        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

        new_config = new_client.config()
        new_config["interceptors"] = list(new_config["interceptors"]) + [
            temporalio.contrib.opentelemetryv2.TracingInterceptor(tracer=provider.get_tracer(__name__))]
        new_client = Client(**new_config)

        async with new_worker(
            new_client,
            BasicTraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            with trace("Research workflow"):
                with custom_span("Research span"):
                    workflow_handle = await new_client.start_workflow(
                        BasicTraceWorkflow.run,
                        id=f"research-workflow-{uuid.uuid4()}",
                        task_queue=worker.task_queue,
                        execution_timeout=timedelta(seconds=120),
                    )
                    workflow_id = workflow_handle.id
                    async def ready() -> bool:
                        return await workflow_handle.query(BasicTraceWorkflow.ready)

                    await assert_eq_eventually(True, ready)

    # Restart the worker with all new objects
    async with AgentEnvironment(model=research_mock_model(), add_temporal_spans=False,
                                use_otel=True) as env:
        new_client = env.applied_on_client(client)

        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

        async with new_worker(
            new_client,
            BasicTraceWorkflow,
            activities=[simple_no_context_activity],
            max_cached_workflows=0,
            task_queue=task_queue,
        ) as worker:
            workflow_handle = new_client.get_workflow_handle(workflow_id)
            await workflow_handle.signal(BasicTraceWorkflow.proceed)
            await workflow_handle.result()

    spans = exporter.get_finished_spans()
    print("\n".join(
        [str({"Name": span.name, "Id": span.context.span_id, "Parent": span.parent.span_id if span.parent else None})
         for span in spans]))
    assert len(spans) == 3
    assert spans[0].parent == None
    assert spans[1].parent.span_id == spans[2].context.span_id
    assert spans[2].parent.span_id == spans[0].context.span_id


async def test_otel_tracing_in_runner(client: Client):
    async with AgentEnvironment(model=research_mock_model(), add_temporal_spans=False, use_otel=True) as env:
        client = env.applied_on_client(client)

        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

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
        print("\n".join([str({"Name": span.name, "Id": span.context.span_id, "Parent": span.parent.span_id if span.parent else None}) for span in spans]))
        assert len(spans) == 13

        assert spans[0].parent.span_id == spans[-1].context.span_id
        for i in range(1,11):
            assert spans[i].parent.span_id == spans[-3].context.span_id
        assert spans[12].parent.span_id == spans[-1].context.span_id


@workflow.defn
class BasicerTraceWorkflow:
    @workflow.run
    async def run(self):
        print("Outside span")
        with temporalio.contrib.opentelemetryv2.workflow.start_as_current_span("Hello World") as span:
            print(span)
            print("Inside span")
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            with temporalio.contrib.opentelemetryv2.workflow.start_as_current_span("Inner") as span:
                await workflow.execute_activity(
                    simple_no_context_activity,
                    start_to_close_timeout=timedelta(seconds=10),
                )
        return

class TemporalSpanProcessor(SimpleSpanProcessor):
    def on_end(self, span: ReadableSpan) -> None:
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            print("Skipping span:", span.get_span_context().span_id, span.start_time)
            return
        super().on_end(span)
