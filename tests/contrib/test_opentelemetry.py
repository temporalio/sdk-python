from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from re import A
from typing import Any, Callable, List, Mapping, Optional

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_tracer
from opentelemetry.util.types import Attributes

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker


@activity.defn
async def tracing_activity(param: str) -> str:
    # TODO(cretz): Call a workflow from inside here
    if not activity.info().is_local:
        activity.heartbeat("details")
    return f"param: {param}"


@workflow.defn
class TracingWorkflow:
    def __init__(self) -> None:
        self.finish = asyncio.Event()

    @workflow.run
    async def run(self, style: str) -> None:
        if style == "continue-as-new":
            return
        if style == "child" or style == "external":
            await self.finish.wait()
            return

        await workflow.execute_activity(
            tracing_activity, "val1", schedule_to_close_timeout=timedelta(seconds=5)
        )
        await workflow.execute_local_activity(
            tracing_activity, "val2", schedule_to_close_timeout=timedelta(seconds=5)
        )
        my_id = workflow.info().workflow_id
        child_handle = await workflow.start_child_workflow(
            TracingWorkflow.run, "child", id=f"{my_id}_child"
        )
        await child_handle.signal(TracingWorkflow.signal, "child-signal-val")
        await child_handle
        # Create another child so we can use it for external handle
        child_handle = await workflow.start_child_workflow(
            TracingWorkflow.run, "external", id=f"{my_id}_external"
        )
        await workflow.get_external_workflow_handle(child_handle.id).signal(
            TracingWorkflow.signal, "external-signal-val"
        )
        await child_handle

        await self.finish.wait()
        workflow.continue_as_new("continue-as-new")

    @workflow.query
    def query(self, param: str) -> str:
        return f"query: {param}"

    @workflow.signal
    def signal(self, param: str) -> None:
        self.finish.set()


@dataclass(frozen=True)
class Span:
    name: str
    children: List[Span] = dataclasses.field(default_factory=list)
    attrs: Attributes = None
    attr_checks: Optional[Mapping[str, Callable[[Optional[Any]], bool]]] = None

    @staticmethod
    def from_span(span: ReadableSpan, spans: List[ReadableSpan]) -> Span:
        return Span(
            name=span.name,
            children=[
                Span.from_span(s, spans)
                for s in spans
                if s.parent and s.parent.span_id == span.context.span_id
            ],
            attrs=span.attributes,
        )

    def dumps(self, indent_depth: int = 0) -> str:
        ret = f"{'  ' * indent_depth}{self.name} (attributes: {self.attrs})"
        for child in self.children:
            ret += "\n" + child.dumps(indent_depth + 1)
        return ret

    def assert_expected(self, expected: Span) -> None:
        assert not expected.attrs
        assert self.name == expected.name
        if self.attrs:
            assert expected.attr_checks and len(expected.attr_checks) == len(self.attrs)
            for k, v in self.attrs.items():
                assert k in expected.attr_checks
                assert expected.attr_checks[k](v)
        else:
            assert not expected.attr_checks
        assert len(expected.children) == len(self.children)
        for expected_child, actual_child in zip(expected.children, self.children):
            actual_child.assert_expected(expected_child)


async def test_opentelemetry_tracing(client: Client):
    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)
    # Create new client with tracer interceptor
    client_config = client.config()
    client_config["interceptors"] = [TracingInterceptor(tracer)]
    client = Client(**client_config)

    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TracingWorkflow],
        activities=[tracing_activity],
    ):
        # Run workflow
        handle = await client.start_workflow(
            TracingWorkflow.run,
            "initial",
            id=f"workflow_{uuid.uuid4()}",
            task_queue=task_queue,
        )
        assert "query: query-val" == await handle.query(
            TracingWorkflow.query, "query-val"
        )
        await handle.signal(TracingWorkflow.signal, "signal-val")
        await handle.result()
        # Run another query after complete to confirm we don't capture replay
        # spans
        assert "query: query-val" == await handle.query(
            TracingWorkflow.query, "query-val"
        )

    spans: List[ReadableSpan] = list(exporter.get_finished_spans())
    actual = [Span.from_span(s, spans) for s in spans if s.parent is None]
    logging.debug("Spans:\n%s", "\n".join(a.dumps() for a in actual))

    check_wf_id = {"temporalWorkflowID": lambda v: v == handle.id}
    check_wf_and_run_id = dict(
        check_wf_id, temporalRunID=lambda v: v == handle.first_execution_run_id
    )
    check_wf_run_and_activity_id = dict(
        check_wf_and_run_id, temporalActivityID=lambda v: v is not None
    )
    check_child_wf_and_run_id = {
        "temporalWorkflowID": lambda v: v == f"{handle.id}_child",
        "temporalRunID": lambda v: v and v != handle.first_execution_run_id,
    }
    check_external_wf_and_run_id = {
        "temporalWorkflowID": lambda v: v == f"{handle.id}_external",
        "temporalRunID": lambda v: v and v != handle.first_execution_run_id,
    }
    check_wf_and_different_run_id = dict(
        check_wf_id, temporalRunID=lambda v: v != handle.first_execution_run_id
    )

    # We expect 4 roots
    assert len(actual) == 4
    # Full workflow span
    actual[0].assert_expected(
        Span(
            name="StartWorkflow:TracingWorkflow",
            attr_checks=check_wf_id,
            children=[
                Span(
                    name="RunWorkflow:TracingWorkflow",
                    attr_checks=check_wf_and_run_id,
                    children=[
                        # Non-local activity
                        Span(
                            name="StartActivity:tracing_activity",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="RunActivity:tracing_activity",
                                    attr_checks=check_wf_run_and_activity_id,
                                )
                            ],
                        ),
                        # Local activity
                        Span(
                            name="StartActivity:tracing_activity",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="RunActivity:tracing_activity",
                                    attr_checks=check_wf_run_and_activity_id,
                                )
                            ],
                        ),
                        # Child workflow
                        Span(
                            name="StartChildWorkflow:TracingWorkflow",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="RunWorkflow:TracingWorkflow",
                                    attr_checks=check_child_wf_and_run_id,
                                )
                            ],
                        ),
                        # Signal child workflow
                        Span(
                            name="SignalChildWorkflow:signal",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="HandleSignal:signal",
                                    attr_checks=check_child_wf_and_run_id,
                                )
                            ],
                        ),
                        # External workflow
                        Span(
                            name="StartChildWorkflow:TracingWorkflow",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="RunWorkflow:TracingWorkflow",
                                    attr_checks=check_external_wf_and_run_id,
                                )
                            ],
                        ),
                        # Signal external workflow
                        Span(
                            name="SignalExternalWorkflow:signal",
                            attr_checks=check_wf_and_run_id,
                            children=[
                                Span(
                                    name="HandleSignal:signal",
                                    attr_checks=check_external_wf_and_run_id,
                                )
                            ],
                        ),
                        # Continue as new
                        Span(
                            name="RunWorkflow:TracingWorkflow",
                            attr_checks=check_wf_and_different_run_id,
                        ),
                    ],
                )
            ],
        )
    )
    # Query span
    actual[1].assert_expected(
        Span(
            name="QueryWorkflow:query",
            attr_checks=check_wf_id,
            children=[
                Span(
                    name="HandleQuery:query",
                    attr_checks=check_wf_and_run_id,
                ),
            ],
        ),
    )
    # Signal span
    actual[2].assert_expected(
        Span(
            name="SignalWorkflow:signal",
            attr_checks=check_wf_id,
            children=[
                Span(
                    name="HandleSignal:signal",
                    attr_checks=check_wf_and_run_id,
                ),
            ],
        ),
    )
    # Query span after workflow completed
    actual[3].assert_expected(
        Span(
            name="QueryWorkflow:query",
            attr_checks=check_wf_id,
            children=[
                Span(
                    name="HandleQuery:query",
                    attr_checks=check_wf_and_different_run_id,
                ),
            ],
        ),
    )
