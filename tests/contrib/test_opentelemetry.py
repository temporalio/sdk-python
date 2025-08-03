from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import multiprocessing
import multiprocessing.managers
import threading
import typing
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import opentelemetry.trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_current_span, get_tracer

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry import workflow as otel_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import SharedStateManager, UnsandboxedWorkflowRunner, Worker

# Passing through because Python 3.9 has an import bug at
# https://github.com/python/cpython/issues/91351
with workflow.unsafe.imports_passed_through():
    import pytest


@dataclass
class TracingActivityParam:
    heartbeat: bool = True
    fail_until_attempt: Optional[int] = None


@activity.defn
async def tracing_activity(param: TracingActivityParam) -> None:
    if param.heartbeat and not activity.info().is_local:
        activity.heartbeat()
    if param.fail_until_attempt and activity.info().attempt < param.fail_until_attempt:
        raise RuntimeError("intentional failure")


@dataclass
class TracingWorkflowParam:
    actions: List[TracingWorkflowAction]


@dataclass
class TracingWorkflowAction:
    fail_on_non_replay: bool = False
    child_workflow: Optional[TracingWorkflowActionChildWorkflow] = None
    activity: Optional[TracingWorkflowActionActivity] = None
    continue_as_new: Optional[TracingWorkflowActionContinueAsNew] = None
    wait_until_signal_count: int = 0
    wait_and_do_update: bool = False


@dataclass
class TracingWorkflowActionChildWorkflow:
    id: str
    param: TracingWorkflowParam
    signal: bool = False
    external_signal: bool = False
    fail_on_non_replay_before_complete: bool = False


@dataclass
class TracingWorkflowActionActivity:
    param: TracingActivityParam
    local: bool = False
    fail_on_non_replay_before_complete: bool = False


@dataclass
class TracingWorkflowActionContinueAsNew:
    param: TracingWorkflowParam


ready_for_update: asyncio.Semaphore


@dataclass(frozen=True)
class SerialisableSpan:
    @dataclass(frozen=True)
    class SpanContext:
        trace_id: int
        span_id: int

        @classmethod
        def from_span_context(
            cls, context: opentelemetry.trace.SpanContext
        ) -> "SerialisableSpan.SpanContext":
            return cls(
                trace_id=context.trace_id,
                span_id=context.span_id,
            )

        @classmethod
        def from_optional_span_context(
            cls, context: Optional[opentelemetry.trace.SpanContext]
        ) -> Optional["SerialisableSpan.SpanContext"]:
            if context is None:
                return None
            return cls.from_span_context(context)

    @dataclass(frozen=True)
    class Link:
        context: SerialisableSpan.SpanContext
        attributes: Dict[str, Any]

    name: str
    context: Optional[SpanContext]
    parent: Optional[SpanContext]
    attributes: Dict[str, Any]
    links: Sequence[Link]

    @classmethod
    def from_readable_span(cls, span: ReadableSpan) -> "SerialisableSpan":
        return cls(
            name=span.name,
            context=cls.SpanContext.from_optional_span_context(span.context),
            parent=cls.SpanContext.from_optional_span_context(span.parent),
            attributes=dict(span.attributes or {}),
            links=tuple(
                cls.Link(
                    context=cls.SpanContext.from_span_context(link.context),
                    attributes=dict(span.attributes or {}),
                )
                for link in span.links
            ),
        )


@workflow.defn
class TracingWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0
        self._did_update = False

    @workflow.run
    async def run(self, param: TracingWorkflowParam) -> None:
        otel_workflow.completed_span("MyCustomSpan", attributes={"foo": "bar"})
        for action in param.actions:
            if action.fail_on_non_replay:
                await self._raise_on_non_replay()
            if action.child_workflow:
                child_handle = await workflow.start_child_workflow(
                    TracingWorkflow.run,
                    action.child_workflow.param,
                    id=action.child_workflow.id,
                )
                if action.child_workflow.fail_on_non_replay_before_complete:
                    await self._raise_on_non_replay()
                if action.child_workflow.signal:
                    await child_handle.signal(TracingWorkflow.signal)
                if action.child_workflow.external_signal:
                    external_handle: workflow.ExternalWorkflowHandle[
                        TracingWorkflow
                    ] = workflow.get_external_workflow_handle_for(
                        TracingWorkflow.run, workflow_id=child_handle.id
                    )
                    await external_handle.signal(TracingWorkflow.signal)
                await child_handle
            if action.activity:
                retry_policy = RetryPolicy(initial_interval=timedelta(milliseconds=1))
                activity_handle = (
                    workflow.start_local_activity(
                        tracing_activity,
                        action.activity.param,
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=retry_policy,
                    )
                    if action.activity.local
                    else workflow.start_activity(
                        tracing_activity,
                        action.activity.param,
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=retry_policy,
                    )
                )
                if action.activity.fail_on_non_replay_before_complete:
                    await self._raise_on_non_replay()
                await activity_handle
            if action.continue_as_new:
                workflow.continue_as_new(action.continue_as_new.param)
            if action.wait_until_signal_count:
                await workflow.wait_condition(
                    lambda: self._signal_count >= action.wait_until_signal_count
                )
            if action.wait_and_do_update:
                ready_for_update.release()
                await workflow.wait_condition(lambda: self._did_update)

    async def _raise_on_non_replay(self) -> None:
        replaying = workflow.unsafe.is_replaying()
        # We sleep to force a task rollover
        await asyncio.sleep(0.01)
        if not replaying:
            raise RuntimeError("Intentional task failure")

    @workflow.query
    def query(self) -> str:
        # We're gonna do a custom span here
        return "some query"

    @workflow.signal
    def signal(self) -> None:
        self._signal_count += 1

    @workflow.update
    def update(self) -> None:
        self._did_update = True

    @update.validator
    def update_validator(self) -> None:
        pass


async def test_opentelemetry_tracing(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    global ready_for_update
    ready_for_update = asyncio.Semaphore(0)
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
        # Needed so we can wait to send update at the right time
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        # Run workflow with various actions
        workflow_id = f"workflow_{uuid.uuid4()}"
        handle = await client.start_workflow(
            TracingWorkflow.run,
            TracingWorkflowParam(
                actions=[
                    # First fail on replay
                    TracingWorkflowAction(fail_on_non_replay=True),
                    # Wait for a signal
                    TracingWorkflowAction(wait_until_signal_count=1),
                    # Exec activity that fails task before complete
                    TracingWorkflowAction(
                        activity=TracingWorkflowActionActivity(
                            param=TracingActivityParam(fail_until_attempt=2),
                            fail_on_non_replay_before_complete=True,
                        ),
                    ),
                    # Wait for update
                    TracingWorkflowAction(wait_and_do_update=True),
                    # Exec child workflow that fails task before complete
                    TracingWorkflowAction(
                        child_workflow=TracingWorkflowActionChildWorkflow(
                            id=f"{workflow_id}_child",
                            # Exec activity and finish after two signals
                            param=TracingWorkflowParam(
                                actions=[
                                    TracingWorkflowAction(
                                        activity=TracingWorkflowActionActivity(
                                            param=TracingActivityParam(),
                                            local=True,
                                        ),
                                    ),
                                    # Wait for the two signals
                                    TracingWorkflowAction(wait_until_signal_count=2),
                                ]
                            ),
                            signal=True,
                            external_signal=True,
                            fail_on_non_replay_before_complete=True,
                        )
                    ),
                    # Continue as new and run one local activity
                    TracingWorkflowAction(
                        continue_as_new=TracingWorkflowActionContinueAsNew(
                            param=TracingWorkflowParam(
                                # Do a local activity in the continue as new
                                actions=[
                                    TracingWorkflowAction(
                                        activity=TracingWorkflowActionActivity(
                                            param=TracingActivityParam(),
                                            local=True,
                                        ),
                                    )
                                ]
                            )
                        )
                    ),
                ],
            ),
            id=workflow_id,
            task_queue=task_queue,
        )
        # Send query, then signal to move it along
        assert "some query" == await handle.query(TracingWorkflow.query)
        await handle.signal(TracingWorkflow.signal)
        # Wait to send the update until after the things that fail tasks are over, as failing a task while the update
        # is running can mean we execute it twice, which will mess up our spans.
        async with ready_for_update:
            await handle.execute_update(TracingWorkflow.update)
        await handle.result()

    # Dump debug with attributes, but do string assertion test without
    logging.debug(
        "Spans:\n%s",
        "\n".join(dump_spans(exporter.get_finished_spans(), with_attributes=False)),
    )
    assert dump_spans(exporter.get_finished_spans(), with_attributes=False) == [
        "StartWorkflow:TracingWorkflow",
        "  RunWorkflow:TracingWorkflow",
        "  MyCustomSpan",
        "  HandleSignal:signal (links: SignalWorkflow:signal)",
        "  StartActivity:tracing_activity",
        "    RunActivity:tracing_activity",
        "    RunActivity:tracing_activity",
        "  ValidateUpdate:update (links: StartWorkflowUpdate:update)",
        "  HandleUpdate:update (links: StartWorkflowUpdate:update)",
        "  StartChildWorkflow:TracingWorkflow",
        "    RunWorkflow:TracingWorkflow",
        "    MyCustomSpan",
        "    StartActivity:tracing_activity",
        "      RunActivity:tracing_activity",
        "    HandleSignal:signal (links: SignalChildWorkflow:signal)",
        "    HandleSignal:signal (links: SignalExternalWorkflow:signal)",
        "    CompleteWorkflow:TracingWorkflow",
        "  SignalChildWorkflow:signal",
        "  SignalExternalWorkflow:signal",
        "  RunWorkflow:TracingWorkflow",
        "  MyCustomSpan",
        "  StartActivity:tracing_activity",
        "    RunActivity:tracing_activity",
        "  CompleteWorkflow:TracingWorkflow",
        "QueryWorkflow:query",
        "  HandleQuery:query (links: StartWorkflow:TracingWorkflow)",
        "SignalWorkflow:signal",
        "StartWorkflowUpdate:update",
    ]


def dump_spans(
    spans: Iterable[Union[ReadableSpan, SerialisableSpan]],
    *,
    parent_id: Optional[int] = None,
    with_attributes: bool = True,
    indent_depth: int = 0,
) -> List[str]:
    ret: List[str] = []
    for span in spans:
        if (not span.parent and parent_id is None) or (
            span.parent and span.parent.span_id == parent_id
        ):
            span_str = f"{'  ' * indent_depth}{span.name}"
            if with_attributes:
                span_str += f" (attributes: {dict(span.attributes or {})})"
            # Add links
            if span.links:
                span_links: List[str] = []
                for link in span.links:
                    for link_span in spans:
                        if (
                            link_span.context
                            and link_span.context.span_id == link.context.span_id
                        ):
                            span_links.append(link_span.name)
                span_str += f" (links: {', '.join(span_links)})"
            # Signals can duplicate in rare situations, so we make sure not to
            # re-add
            if "Signal" in span_str and span_str in ret:
                continue
            ret.append(span_str)
            ret += dump_spans(
                spans,
                parent_id=(span.context.span_id if span.context else None),
                with_attributes=with_attributes,
                indent_depth=indent_depth + 1,
            )
    return ret


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "done"


async def test_opentelemetry_always_create_workflow_spans(client: Client):
    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)

    # Create a worker with an interceptor without always create
    async with Worker(
        client,
        task_queue=f"task_queue_{uuid.uuid4()}",
        workflows=[SimpleWorkflow],
        interceptors=[TracingInterceptor(tracer)],
    ) as worker:
        assert "done" == await client.execute_workflow(
            SimpleWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Confirm the spans are not there
    spans = exporter.get_finished_spans()
    logging.debug("Spans:\n%s", "\n".join(dump_spans(spans, with_attributes=False)))
    assert len(spans) == 0

    # Now create a worker with an interceptor with always create
    async with Worker(
        client,
        task_queue=f"task_queue_{uuid.uuid4()}",
        workflows=[SimpleWorkflow],
        interceptors=[TracingInterceptor(tracer, always_create_workflow_spans=True)],
    ) as worker:
        assert "done" == await client.execute_workflow(
            SimpleWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Confirm the spans are not there
    spans = exporter.get_finished_spans()
    logging.debug("Spans:\n%s", "\n".join(dump_spans(spans, with_attributes=False)))
    assert len(spans) > 0
    assert spans[0].name == "RunWorkflow:SimpleWorkflow"


# TODO(cretz): Additional tests to write
# * query without interceptor (no headers)
# * workflow without interceptor (no headers) but query with interceptor (headers)
# * workflow failure and wft failure
# * signal with start
# * signal failure and wft failure from signal


@workflow.defn
class ActivityTracePropagationWorkflow:
    @workflow.run
    async def run(self) -> str:
        retry_policy = RetryPolicy(initial_interval=timedelta(milliseconds=1))
        return await workflow.execute_activity(
            sync_activity,
            {},
            # TODO: Reduce to 10s - increasing to make debugging easier
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=retry_policy,
        )


@activity.defn
def sync_activity(param: typing.Any) -> str:
    """An activity that uses tracing features.

    When executed in a process pool, we expect the trace context to be available
    from the parent process.
    """
    inner_tracer = get_tracer("sync_activity")
    with inner_tracer.start_as_current_span(
        "child_span",
    ):
        return "done"


async def test_activity_trace_propagation(
    client: Client,
    env: WorkflowEnvironment,
):
    # TODO: add spy interceptor to check `input.fn` wraps original metadata
    # TODO: Add Resource to show how resource would be propagated

    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)

    # Create a proxy list using the server process manager which we'll use
    # to access finished spans in the process pool
    manager = multiprocessing.Manager()
    finished_spans_proxy = typing.cast(
        multiprocessing.managers.ListProxy[SerialisableSpan], manager.list()
    )

    # Create a worker with a process pool activity executor
    async with Worker(
        client,
        task_queue=f"task_queue_{uuid.uuid4()}",
        workflows=[ActivityTracePropagationWorkflow],
        activities=[sync_activity],
        interceptors=[TracingInterceptor(tracer)],
        activity_executor=concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            initializer=activity_trace_propagation_initializer,
            initargs=(finished_spans_proxy,),
        ),
        shared_state_manager=SharedStateManager.create_from_multiprocessing(manager),
    ) as worker:
        assert "done" == await client.execute_workflow(
            ActivityTracePropagationWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    spans = exporter.get_finished_spans() + tuple(finished_spans_proxy)
    logging.debug("Spans:\n%s", "\n".join(dump_spans(spans, with_attributes=False)))
    assert dump_spans(spans, with_attributes=False) == [
        "RunActivity:sync_activity",
        "  child_span",
    ]


class _ListProxySpanExporter(SpanExporter):
    """Implementation of :class:`SpanExporter` that exports spans to a
    list proxy created by a multiprocessing manager.

    This class is used for testing multiprocessing setups, as we can get access
    to the finished spans from the parent process.

    In production, you would use `OTLPSpanExporter` or similar to export spans.
    Tracing is designed to be distributed, the child process can push collected
    spans directly to a collector or backend, which can reassemble the spans
    into a single trace.
    """

    def __init__(
        self, finished_spans: multiprocessing.managers.ListProxy[SerialisableSpan]
    ) -> None:
        self._finished_spans = finished_spans
        self._stopped = False
        self._lock = threading.Lock()

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        if self._stopped:
            return SpanExportResult.FAILURE
        with self._lock:
            # Note: ReadableSpan is not picklable, so convert to a DTO
            # Note: we could use `span.to_json()` but there isn't a `from_json`
            # and the serialisation isn't easily reversible, e.g. `parent` context
            # is lost, span/trace IDs are transformed into strings
            self._finished_spans.extend(
                [SerialisableSpan.from_readable_span(span) for span in spans]
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._stopped = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def activity_trace_propagation_initializer(
    _finished_spans_proxy: multiprocessing.managers.ListProxy[SerialisableSpan],
) -> None:
    """Initializer for the process pool worker to export spans to a shared list."""
    _exporter = _ListProxySpanExporter(_finished_spans_proxy)
    _provider = TracerProvider()
    _provider.add_span_processor(SimpleSpanProcessor(_exporter))
    opentelemetry.trace.set_tracer_provider(_provider)
