from __future__ import annotations

import asyncio
import contextvars
import gc
import logging
import queue
import threading
import uuid
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ParamSpec, TypeVar, cast

import nexusrpc
import opentelemetry.context
import opentelemetry.trace
import pytest
from opentelemetry import baggage, context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, get_tracer

from temporalio import activity, nexus, workflow
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateStage
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.contrib.opentelemetry import (
    OpenTelemetryInterceptor,
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
    create_tracer_provider,
)
from temporalio.contrib.opentelemetry import workflow as otel_workflow
from temporalio.contrib.opentelemetry._context import AttachedContext
from temporalio.contrib.opentelemetry._otel_interceptor import (
    _TracingWorkflowInboundInterceptor as _OtelTracingWorkflowInboundInterceptor,
)
from temporalio.exceptions import (
    ApplicationError,
    ApplicationErrorCategory,
    NexusOperationError,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from tests.helpers import LogCapturer
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class TracingActivityParam:
    heartbeat: bool = True
    fail_until_attempt: int | None = None


@activity.defn
async def tracing_activity(param: TracingActivityParam) -> None:
    if param.heartbeat and not activity.info().is_local:
        activity.heartbeat()
    if param.fail_until_attempt and activity.info().attempt < param.fail_until_attempt:
        raise RuntimeError("intentional failure")


@dataclass
class TracingWorkflowParam:
    actions: list[TracingWorkflowAction]


@dataclass
class TracingWorkflowAction:
    fail_on_non_replay: bool = False
    child_workflow: TracingWorkflowActionChildWorkflow | None = None
    activity: TracingWorkflowActionActivity | None = None
    continue_as_new: TracingWorkflowActionContinueAsNew | None = None
    wait_until_signal_count: int = 0
    wait_and_do_update: bool = False
    wait_and_do_start_with_update: bool = False
    start_and_cancel_nexus_operation: bool = False


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


@workflow.defn
class SignalWithStartHeaderWorkflow:
    def __init__(self) -> None:
        self._signaled = False

    @workflow.run
    async def run(self) -> bool:
        await workflow.wait_condition(lambda: self._signaled)
        return "_tracer-data" in workflow.info().headers

    @workflow.signal
    def notify(self) -> None:
        self._signaled = True


@workflow.defn
class SignalWithStartCallerWorkflow:
    @workflow.run
    async def run(self, target_id: str, task_queue: str) -> str:
        handle = await workflow.signal_with_start_workflow(
            SignalWithStartHeaderWorkflow.run,
            id=target_id,
            task_queue=task_queue,
            signal=SignalWithStartHeaderWorkflow.notify,
        )
        return handle.id


@dataclass
class TracingWorkflowActionContinueAsNew:
    param: TracingWorkflowParam


@workflow.defn
class ExpectCancelNexusWorkflow:
    @workflow.run
    async def run(self, _input: str):
        try:
            await asyncio.wait_for(asyncio.Future(), 2)
        except asyncio.TimeoutError:
            raise ApplicationError("expected cancellation")


@nexusrpc.handler.service_handler
class InterceptedNexusService:
    @nexus.workflow_run_operation
    async def intercepted_operation(
        self, ctx: nexus.WorkflowRunOperationContext, input: str
    ) -> nexus.WorkflowHandle[None]:
        return await ctx.start_workflow(
            ExpectCancelNexusWorkflow.run,
            input,
            id=f"wf-{uuid.uuid4()}-{ctx.request_id}",
        )


ready_for_update: asyncio.Semaphore
ready_for_update_with_start: asyncio.Semaphore


@workflow.defn
class TracingWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0
        self._did_update = False
        self._did_update_with_start = False

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
            if action.wait_and_do_start_with_update:
                ready_for_update_with_start.release()
                await workflow.wait_condition(lambda: self._did_update_with_start)
            if action.start_and_cancel_nexus_operation:
                nexus_client = workflow.create_nexus_client(
                    endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
                    service=InterceptedNexusService,
                )

                nexus_handle = await nexus_client.start_operation(
                    operation=InterceptedNexusService.intercepted_operation,
                    input="nexus-workflow",
                )
                nexus_handle.cancel()

                try:
                    await nexus_handle
                except NexusOperationError:
                    pass

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

    @workflow.update
    def update_with_start(self) -> None:
        self._did_update_with_start = True

    @update.validator
    def update_validator(self) -> None:
        pass


@pytest.mark.requires_local_server
async def test_workflow_signal_with_start_propagates_trace_headers(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)
    config = client.config()
    config["interceptors"] = [TracingInterceptor(tracer)]
    client = Client(**config)

    async with Worker(
        client,
        task_queue=f"signal-with-start-{uuid.uuid4()}",
        workflows=[SignalWithStartCallerWorkflow, SignalWithStartHeaderWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ) as worker:
        target_id = f"signal-with-start-target-{uuid.uuid4()}"
        with tracer.start_as_current_span("signal-with-start"):
            caller = await client.start_workflow(
                SignalWithStartCallerWorkflow.run,
                args=[target_id, worker.task_queue],
                id=f"signal-with-start-caller-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert await caller.result() == target_id
        assert await client.get_workflow_handle(target_id).result() is True
    assert any(
        span.name == "SignalWithStartWorkflow" for span in exporter.get_finished_spans()
    )


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


async def test_opentelemetry_tracing_update_with_start(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    global ready_for_update_with_start
    ready_for_update_with_start = asyncio.Semaphore(0)
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
        workflow_params = TracingWorkflowParam(
            actions=[
                # Wait for update
                TracingWorkflowAction(wait_and_do_start_with_update=True),
            ]
        )
        handle = await client.start_workflow(
            TracingWorkflow.run,
            workflow_params,
            id=workflow_id,
            task_queue=task_queue,
        )
        async with ready_for_update_with_start:
            start_op = WithStartWorkflowOperation(
                TracingWorkflow.run,
                workflow_params,
                id=handle.id,
                task_queue=task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            await client.start_update_with_start_workflow(
                TracingWorkflow.update_with_start,
                start_workflow_operation=start_op,
                id=handle.id,
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )
        await handle.result()

        # issue update with start again to trigger a new workflow
        workflow_id = f"workflow_{uuid.uuid4()}"
        start_op = WithStartWorkflowOperation(
            TracingWorkflow.run,
            TracingWorkflowParam(actions=[]),
            id=workflow_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        await client.execute_update_with_start_workflow(
            update=TracingWorkflow.update_with_start,
            start_workflow_operation=start_op,
            id=workflow_id,
        )

    # Dump debug with attributes, but do string assertion test without
    logging.debug(
        "Spans:\n%s",
        "\n".join(dump_spans(exporter.get_finished_spans(), with_attributes=False)),
    )
    assert dump_spans(exporter.get_finished_spans(), with_attributes=False) == [
        "StartWorkflow:TracingWorkflow",
        "  RunWorkflow:TracingWorkflow",
        "  MyCustomSpan",
        "  HandleUpdate:update_with_start (links: StartUpdateWithStartWorkflow:TracingWorkflow)",
        "  CompleteWorkflow:TracingWorkflow",
        "StartUpdateWithStartWorkflow:TracingWorkflow",
        "StartUpdateWithStartWorkflow:TracingWorkflow",
        "  HandleUpdate:update_with_start (links: StartUpdateWithStartWorkflow:TracingWorkflow)",
        "  RunWorkflow:TracingWorkflow",
        "  MyCustomSpan",
        "  CompleteWorkflow:TracingWorkflow",
    ]


@pytest.mark.requires_local_server
async def test_opentelemetry_tracing_nexus(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    global ready_for_update_with_start
    ready_for_update_with_start = asyncio.Semaphore(0)
    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)
    # Create new client with tracer interceptor
    client_config = client.config()
    client_config["interceptors"] = [TracingInterceptor(tracer)]
    client = Client(**client_config)

    task_queue = f"task-queue-{uuid.uuid4()}"
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TracingWorkflow, ExpectCancelNexusWorkflow],
        activities=[tracing_activity],
        nexus_service_handlers=[InterceptedNexusService()],
        # Needed so we can wait to send update at the right time
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        # Run workflow with various actions
        workflow_id = f"workflow_{uuid.uuid4()}"
        workflow_params = TracingWorkflowParam(
            actions=[
                TracingWorkflowAction(start_and_cancel_nexus_operation=True),
            ]
        )
        handle = await client.start_workflow(
            TracingWorkflow.run,
            workflow_params,
            id=workflow_id,
            task_queue=task_queue,
        )
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
        "  StartNexusOperation:InterceptedNexusService/intercepted_operation",
        "    RunStartNexusOperationHandler:InterceptedNexusService/intercepted_operation",
        "      StartWorkflow:ExpectCancelNexusWorkflow",
        "        RunWorkflow:ExpectCancelNexusWorkflow",
        "    RunCancelNexusOperationHandler:InterceptedNexusService/intercepted_operation",
        "  CompleteWorkflow:TracingWorkflow",
    ]


def dump_spans(
    spans: Iterable[ReadableSpan],
    *,
    parent_id: int | None = None,
    with_attributes: bool = True,
    indent_depth: int = 0,
) -> list[str]:
    ret: list[str] = []
    for span in spans:
        if (not span.parent and parent_id is None) or (
            span.parent and span.parent.span_id == parent_id
        ):
            span_str = f"{'  ' * indent_depth}{span.name}"
            if with_attributes:
                span_str += f" (attributes: {dict(span.attributes or {})})"
            # Add links
            if span.links:
                span_links: list[str] = []
                for link in span.links:
                    for link_span in spans:
                        if (
                            link_span.context is not None
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
                parent_id=span.context.span_id if span.context else None,
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


attempted = False


@activity.defn
def benign_activity() -> str:
    global attempted
    if attempted:
        return "done"
    attempted = True
    raise ApplicationError(
        category=ApplicationErrorCategory.BENIGN, message="Benign Error"
    )


@workflow.defn
class BenignWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            benign_activity, schedule_to_close_timeout=timedelta(seconds=1)
        )


async def test_opentelemetry_benign_exception(client: Client):
    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)

    # Create new client with tracer interceptor
    client_config = client.config()
    client_config["interceptors"] = [TracingInterceptor(tracer)]
    client = Client(**client_config)

    async with Worker(
        client,
        task_queue=f"task_queue_{uuid.uuid4()}",
        workflows=[BenignWorkflow],
        activities=[benign_activity],
        activity_executor=ThreadPoolExecutor(max_workers=1),
    ) as worker:
        assert "done" == await client.execute_workflow(
            BenignWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=worker.task_queue,
            retry_policy=RetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(milliseconds=10)
            ),
        )
    spans = exporter.get_finished_spans()
    assert all(span.status.status_code == StatusCode.UNSET for span in spans)


@contextmanager
def baggage_values(values: dict[str, str]) -> Generator[None]:
    ctx = context.get_current()
    for key, value in values.items():
        ctx = baggage.set_baggage(key, value, context=ctx)

    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


@pytest.fixture
def client_with_tracing(client: Client) -> Client:
    tracer = get_tracer(__name__, tracer_provider=TracerProvider())
    client_config = client.config()
    client_config["interceptors"] = [TracingInterceptor(tracer)]
    return Client(**client_config)


def get_baggage_value(key: str) -> str:
    return cast("str", baggage.get_baggage(key))


@activity.defn
async def read_baggage_activity() -> dict[str, str]:
    return {
        "user_id": get_baggage_value("user.id"),
        "tenant_id": get_baggage_value("tenant.id"),
    }


@workflow.defn
class ReadBaggageTestWorkflow:
    @workflow.run
    async def run(self) -> dict[str, str]:
        return await workflow.execute_activity(
            read_baggage_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_opentelemetry_baggage_propagation_basic(client_with_tracing: Client):
    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client_with_tracing,
        task_queue=task_queue,
        workflows=[ReadBaggageTestWorkflow],
        activities=[read_baggage_activity],
    ):
        with baggage_values({"user.id": "test-user-123", "tenant.id": "some-corp"}):
            result = await client_with_tracing.execute_workflow(
                ReadBaggageTestWorkflow.run,
                id=f"workflow_{uuid.uuid4()}",
                task_queue=task_queue,
            )

        assert result["user_id"] == "test-user-123", (
            "user.id baggage should propagate to activity"
        )
        assert result["tenant_id"] == "some-corp", (
            "tenant.id baggage should propagate to activity"
        )


@activity.defn
async def read_baggage_local_activity() -> dict[str, str]:
    return {
        "user_id": get_baggage_value("user.id"),
        "tenant_id": get_baggage_value("tenant.id"),
    }


@workflow.defn
class LocalActivityBaggageTestWorkflow:
    @workflow.run
    async def run(self) -> dict[str, str]:
        return await workflow.execute_local_activity(
            read_baggage_local_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_opentelemetry_baggage_propagation_local_activity(
    client_with_tracing: Client,
):
    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client_with_tracing,
        task_queue=task_queue,
        workflows=[LocalActivityBaggageTestWorkflow],
        activities=[read_baggage_local_activity],
    ):
        with baggage_values(
            {
                "user.id": "test-user-456",
                "tenant.id": "local-corp",
            }
        ):
            result = await client_with_tracing.execute_workflow(
                LocalActivityBaggageTestWorkflow.run,
                id=f"workflow_{uuid.uuid4()}",
                task_queue=task_queue,
            )

        assert result["user_id"] == "test-user-456"
        assert result["tenant_id"] == "local-corp"


retry_attempt_baggage_values: list[str] = []


@activity.defn
async def failing_baggage_activity() -> None:
    retry_attempt_baggage_values.append(get_baggage_value("user.id"))
    if activity.info().attempt < 2:
        raise RuntimeError("Intentional failure")


@workflow.defn
class RetryBaggageTestWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            failing_baggage_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=1)),
        )


async def test_opentelemetry_baggage_propagation_with_retries(
    client_with_tracing: Client,
) -> None:
    global retry_attempt_baggage_values
    retry_attempt_baggage_values = []

    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client_with_tracing,
        task_queue=task_queue,
        workflows=[RetryBaggageTestWorkflow],
        activities=[failing_baggage_activity],
    ):
        with baggage_values({"user.id": "test-user-retry"}):
            await client_with_tracing.execute_workflow(
                RetryBaggageTestWorkflow.run,
                id=f"workflow_{uuid.uuid4()}",
                task_queue=task_queue,
            )

        # Verify baggage was present on all attempts
        assert len(retry_attempt_baggage_values) == 2
        assert all(v == "test-user-retry" for v in retry_attempt_baggage_values)


@activity.defn
async def context_clear_noop_activity() -> None:
    pass


@activity.defn
async def context_clear_exception_activity() -> None:
    raise Exception("Simulated exception")


@workflow.defn
class ContextClearWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            context_clear_noop_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                maximum_attempts=1, initial_interval=timedelta(milliseconds=1)
            ),
        )


@pytest.mark.parametrize(
    "activity,expect_failure",
    [
        (context_clear_noop_activity, not True),
        (context_clear_exception_activity, True),
    ],
)
async def test_opentelemetry_context_restored_after_activity(
    client_with_tracing: Client,
    activity: Callable[[], None],
    expect_failure: bool,
) -> None:
    attach_count = 0
    detach_count = 0
    original_attach = context.attach
    original_detach = context.detach
    original_attached_detach = AttachedContext.detach

    def tracked_attach(ctx):  # type:ignore[reportMissingParameterType]
        nonlocal attach_count
        attach_count += 1
        return original_attach(ctx)

    def tracked_detach(token):  # type:ignore[reportMissingParameterType]
        nonlocal detach_count
        detach_count += 1
        return original_detach(token)

    # Spans detach through context.detach; the interceptors reset their own
    # tokens directly, so count those detaches where they happen.
    def tracked_attached_detach(self: AttachedContext) -> bool:
        nonlocal detach_count
        detached = original_attached_detach(self)
        if detached:
            detach_count += 1
        return detached

    context.attach = tracked_attach
    context.detach = tracked_detach
    AttachedContext.detach = tracked_attached_detach

    try:
        task_queue = f"task_queue_{uuid.uuid4()}"
        async with Worker(
            client_with_tracing,
            task_queue=task_queue,
            workflows=[ContextClearWorkflow],
            activities=[activity],
        ):
            with baggage_values({"user.id": "test-123"}):
                try:
                    await client_with_tracing.execute_workflow(
                        ContextClearWorkflow.run,
                        id=f"workflow_{uuid.uuid4()}",
                        task_queue=task_queue,
                    )
                    assert not expect_failure, (
                        "This test should have raised an exception"
                    )
                except Exception:
                    assert expect_failure, "This test is not expeced to raise"

        assert attach_count == detach_count, (
            f"Context leak detected: {attach_count} attaches vs {detach_count} detaches. "
        )
        assert attach_count > 0, "Expected at least one context attach/detach"

    finally:
        context.attach = original_attach
        context.detach = original_detach
        AttachedContext.detach = original_attached_detach


@activity.defn
async def simple_no_context_activity() -> str:
    return "success"


@workflow.defn
class SimpleNoContextWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            simple_no_context_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_opentelemetry_interceptor_works_if_no_context(
    client_with_tracing: Client,
):
    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client_with_tracing,
        task_queue=task_queue,
        workflows=[SimpleNoContextWorkflow],
        activities=[simple_no_context_activity],
    ):
        result = await client_with_tracing.execute_workflow(
            SimpleNoContextWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=task_queue,
        )

        assert result == "success"


# TODO(cretz): Additional tests to write
# * query without interceptor (no headers)
# * workflow without interceptor (no headers) but query with interceptor (headers)
# * workflow failure and wft failure
# * signal with start
# * signal failure and wft failure from signal


async def test_opentelemetry_standalone_activity_tracing(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)
    client_config = client.config()
    client_config["interceptors"] = [TracingInterceptor(tracer)]
    client = Client(**client_config)

    task_queue = f"task_queue_{uuid.uuid4()}"
    activity_id = f"activity_{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[tracing_activity],
    ):
        handle = await client.start_activity(
            tracing_activity,
            TracingActivityParam(heartbeat=False),
            id=activity_id,
            task_queue=task_queue,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        await handle.result()

    finished_spans = exporter.get_finished_spans()
    assert dump_spans(finished_spans, with_attributes=False) == [
        "StartActivity:tracing_activity",
        "  RunActivity:tracing_activity",
    ]
    start_activity_span = next(
        s for s in finished_spans if s.name == "StartActivity:tracing_activity"
    )
    assert start_activity_span.attributes is not None
    assert start_activity_span.attributes["temporalActivityID"] == activity_id
    assert start_activity_span.attributes["temporalActivityType"] == "tracing_activity"


def _v1_workflow_context(success_is_complete: bool = True) -> Any:
    class _fake_self:
        def _load_workflow_context_carrier(*_args):
            return None

        def _set_on_context(self, ctx: Any):
            return opentelemetry.context.set_value("test-key", "test-value", ctx)

        def _completed_span(*args: Any, **_kwargs: Any):
            pass

    return TracingWorkflowInboundInterceptor._top_level_workflow_context(
        _fake_self(),  # type: ignore
        success_is_complete=success_is_complete,
    )


def _v2_workflow_context() -> Any:
    class _fake_input:
        headers: dict[str, Any] = {}

    return _OtelTracingWorkflowInboundInterceptor._top_level_workflow_context(
        object(),  # type: ignore
        _fake_input(),  # type: ignore
    )


def _assert_context_detach_is_safe(make_context_manager: Callable[[], Any]) -> None:
    # create a context manager and force enter to happen on this thread
    context_manager = make_context_manager()
    context_manager.__enter__()

    # move reference to context manager into queue
    q: queue.Queue = queue.Queue()
    q.put(context_manager)
    del context_manager

    def worker():
        # pull reference from queue and delete the last reference
        context_manager = q.get()
        del context_manager
        # force gc
        gc.collect()

    with LogCapturer().logs_captured(opentelemetry.context.logger) as capturer:
        # run forced gc on other thread so exit happens there
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        def otel_context_error(record: logging.LogRecord) -> bool:
            return (
                record.name == "opentelemetry.context"
                and "Failed to detach context" in record.message
            )

        assert capturer.find(otel_context_error) is None, (
            "Detach from context message should not be logged"
        )


@pytest.mark.parametrize(
    "make_context_manager",
    [_v1_workflow_context, _v2_workflow_context],
    ids=["TracingInterceptor", "OpenTelemetryInterceptor"],
)
@pytest.mark.parametrize(
    "threading_instrumented", [False, True], ids=["plain", "threading-instrumented"]
)
def test_opentelemetry_safe_detach(
    make_context_manager: Callable[[], Any], threading_instrumented: bool
):
    if not threading_instrumented:
        _assert_context_detach_is_safe(make_context_manager)
        return
    # OpenTelemetry's threading instrumentation (strands turns it on when an
    # Agent is created) propagates the current Context object into new
    # threads, so a context-identity check alone would detach a token minted
    # on another thread.
    threading_instrumentation = pytest.importorskip(
        "opentelemetry.instrumentation.threading"
    )
    instrumentor = threading_instrumentation.ThreadingInstrumentor()
    already_instrumented = instrumentor.is_instrumented_by_opentelemetry
    if not already_instrumented:
        instrumentor.instrument()
    try:
        _assert_context_detach_is_safe(make_context_manager)
    finally:
        if not already_instrumented:
            instrumentor.uninstrument()


def _run_in_context_on_new_thread(
    ctx: contextvars.Context, fn: Callable[[], Any]
) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, fn).result(timeout=5)


@pytest.mark.parametrize(
    "make_context_manager",
    [lambda: _v1_workflow_context(success_is_complete=False), _v2_workflow_context],
    ids=["TracingInterceptor", "OpenTelemetryInterceptor"],
)
def test_opentelemetry_detach_after_thread_change(
    make_context_manager: Callable[[], Any],
):
    # Workflow activations run on a thread pool, so the asyncio task that
    # entered one of these context managers can leave it on another thread
    # while keeping the same contextvars.Context. The token is valid there and
    # the detach must restore the outer context.
    task_context = contextvars.copy_context()
    outer = opentelemetry.context.set_value("outer", True)
    task_context.run(opentelemetry.context.attach, outer)
    context_manager = make_context_manager()

    _run_in_context_on_new_thread(task_context, context_manager.__enter__)
    assert task_context.run(opentelemetry.context.get_current) is not outer
    _run_in_context_on_new_thread(
        task_context, lambda: context_manager.__exit__(None, None, None)
    )
    assert task_context.run(opentelemetry.context.get_current) is outer


_P = ParamSpec("_P")
_T = TypeVar("_T")


class _AlternatingThreadExecutor(ThreadPoolExecutor):
    """Runs each submission on a different thread than the one before it, the
    way successive activations of a workflow can land on different pool
    threads under load."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self._pools = [ThreadPoolExecutor(max_workers=1) for _ in range(2)]
        self._submissions = 0

    def submit(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> Future[_T]:
        pool = self._pools[self._submissions % len(self._pools)]
        self._submissions += 1
        return pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        for pool in self._pools:
            pool.shutdown(wait, cancel_futures=cancel_futures)
        super().shutdown(wait, cancel_futures=cancel_futures)


@workflow.defn
class TimerWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Two activations: the start, and the timer firing.
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "make_interceptor",
    [lambda: TracingInterceptor(get_tracer(__name__)), OpenTelemetryInterceptor],
    ids=["TracingInterceptor", "OpenTelemetryInterceptor"],
)
async def test_opentelemetry_context_restored_after_activation_thread_change(
    client: Client,
    make_interceptor: Callable[[], Interceptor],
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    # OpenTelemetryInterceptor insists on a replay-safe global provider.
    opentelemetry.trace.set_tracer_provider(create_tracer_provider())
    records: list[tuple[threading.Thread, threading.Thread, bool]] = []

    class ContextRestoredWorkflowInboundInterceptor(WorkflowInboundInterceptor):
        async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
            entered_on = threading.current_thread()
            before = opentelemetry.context.get_current()
            result = await super().execute_workflow(input)
            records.append(
                (
                    entered_on,
                    threading.current_thread(),
                    opentelemetry.context.get_current() is before,
                )
            )
            return result

    class ContextRestoredInterceptor(Interceptor):
        def workflow_interceptor_class(
            self, input: WorkflowInterceptorClassInput
        ) -> type[WorkflowInboundInterceptor] | None:
            return ContextRestoredWorkflowInboundInterceptor

    executor = _AlternatingThreadExecutor()
    try:
        async with Worker(
            client,
            task_queue=f"task_queue_{uuid.uuid4()}",
            workflows=[TimerWorkflow],
            # The first interceptor is the outermost, so it sees whatever the
            # tracing interceptor leaves attached.
            interceptors=[ContextRestoredInterceptor(), make_interceptor()],
            workflow_task_executor=executor,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ) as worker:
            await client.execute_workflow(
                TimerWorkflow.run,
                id=f"workflow_{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
    finally:
        executor.shutdown()

    assert len(records) == 1
    entered_on, left_on, context_restored = records[0]
    assert entered_on is not left_on, (
        "The workflow was expected to change threads between its activations"
    )
    assert context_restored, (
        "The tracing interceptor must detach its context even though the "
        "workflow left it on a different thread than it entered on"
    )
