import traceback
import uuid
from datetime import timedelta

import opentelemetry.trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import  get_current_span

import temporalio.contrib.opentelemetryv2
from temporalio.contrib.opentelemetryv2 import TemporalIdGenerator

from temporalio import workflow, activity
from temporalio.client import Client
from tests.helpers import new_worker
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

@activity.defn
async def simple_no_context_activity() -> str:
    provider = trace_sdk.TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("Activity") as span:
        print("Activity Span:", span)
        pass

    spans = exporter.get_finished_spans()
    print("Completed Activity Spans:")
    print("\n".join(
        [str({"Name": span.name, "Id": span.context.span_id, "Parent": span.parent.span_id if span.parent else None})
         for span in spans]))
    return "success"

@workflow.defn
class BasicTraceWorkflow:
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
            with temporalio.contrib.opentelemetryv2.workflow.start_as_current_span("Inner"):
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

async def test_otel_tracing_parent_trace(client: Client):
    exporter = InMemorySpanExporter()

    generator = TemporalIdGenerator()
    provider = trace_sdk.TracerProvider(id_generator=generator)
    provider.add_span_processor(TemporalSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    new_config = client.config()
    new_config["interceptors"] = list(new_config["interceptors"]) + [
        temporalio.contrib.opentelemetryv2.TracingInterceptor(tracer=tracer)]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        BasicTraceWorkflow,
        activities=[simple_no_context_activity],
        max_cached_workflows=0,
    ) as worker:
        with tracer.start_as_current_span("Research workflow") as span:
            print(span.get_span_context().span_id)
            print("Current span in worker code:", get_current_span())
            workflow_handle = await new_client.start_workflow(
                BasicTraceWorkflow.run,
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            await workflow_handle.result()
    #
    spans = exporter.get_finished_spans()
    print("Completed Spans:")
    print("\n".join(
        [str({"Name": span.name, "Id": span.context.span_id, "Parent": span.parent.span_id if span.parent else None})
         for span in spans]))
    # assert len(spans) == 3
    # assert spans[0].parent == None
    # assert spans[1].parent.span_id == spans[2].context.span_id
    # assert spans[2].parent.span_id == spans[0].context.span_id
