from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, List, Optional

import opentelemetry.context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, get_tracer

from temporalio import activity, workflow
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateStage
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry import workflow as otel_workflow
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from tests.helpers import LogCapturer
from tests.helpers.cache_eviction import (
    CacheEvictionTearDownWorkflow,
    WaitForeverWorkflow,
    wait_forever_activity,
)

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
    wait_and_do_start_with_update: bool = False


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


def dump_spans(
    spans: Iterable[ReadableSpan],
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


# TODO(cretz): Additional tests to write
# * query without interceptor (no headers)
# * workflow without interceptor (no headers) but query with interceptor (headers)
# * workflow failure and wft failure
# * signal with start
# * signal failure and wft failure from signal


async def test_opentelemetry_safe_detach(client: Client):
    # This test simulates forcing eviction. This purposely raises GeneratorExit on
    # GC which triggers the finally which could run on any thread Python
    # chooses. When this occurs, we should not detach the token from the context
    # b/c the context no longer exists

    # Create a tracer that has an in-memory exporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = get_tracer(__name__, tracer_provider=provider)

    async with Worker(
        client,
        workflows=[CacheEvictionTearDownWorkflow, WaitForeverWorkflow],
        activities=[wait_forever_activity],
        max_cached_workflows=0,
        task_queue=f"task_queue_{uuid.uuid4()}",
        disable_safe_workflow_eviction=True,
        interceptors=[TracingInterceptor(tracer)],
    ) as worker:
        # Put a hook to catch unraisable exceptions
        old_hook = sys.unraisablehook
        hook_calls: List[sys.UnraisableHookArgs] = []
        sys.unraisablehook = hook_calls.append

        with LogCapturer().logs_captured(opentelemetry.context.logger) as capturer:
            try:
                handle = await client.start_workflow(
                    CacheEvictionTearDownWorkflow.run,
                    id=f"wf-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

                # CacheEvictionTearDownWorkflow requires 3 signals to be sent
                await handle.signal(CacheEvictionTearDownWorkflow.signal)
                await handle.signal(CacheEvictionTearDownWorkflow.signal)
                await handle.signal(CacheEvictionTearDownWorkflow.signal)

                await handle.result()
            finally:
                sys.unraisablehook = old_hook

            # Confirm at least 1 exception
            if len(hook_calls) < 1:
                logging.warning(
                    "Expected at least 1 exception. Unable to properly verify context detachment"
                )

            def otel_context_error(record: logging.LogRecord) -> bool:
                return (
                    record.name == "opentelemetry.context"
                    and "Failed to detach context" in record.message
                )

            assert (
                capturer.find(otel_context_error) is None
            ), "Detach from context message should not be logged"
