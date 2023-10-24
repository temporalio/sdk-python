from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, List, Optional

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_tracer

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry import workflow as otel_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


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


@workflow.defn
class TracingWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0

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
    def update(self) -> int:
        self._signal_count += 1
        return self._signal_count

    @update.validator
    def update_validator(self) -> None:
        print("Actually in validator")
        pass


async def test_opentelemetry_tracing(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
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
        # Run workflow with various actions
        workflow_id = f"workflow_{uuid.uuid4()}"
        handle = await client.start_workflow(
            TracingWorkflow.run,
            TracingWorkflowParam(
                actions=[
                    # First fail on replay
                    TracingWorkflowAction(fail_on_non_replay=True),
                    # Wait for a signal & update
                    TracingWorkflowAction(wait_until_signal_count=2),
                    # Exec activity that fails task before complete
                    TracingWorkflowAction(
                        activity=TracingWorkflowActionActivity(
                            param=TracingActivityParam(fail_until_attempt=2),
                            fail_on_non_replay_before_complete=True,
                        ),
                    ),
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
        "  HandleUpdateValidator:update (links: StartWorkflowUpdate:update)",
        "  HandleUpdateHandler:update (links: StartWorkflowUpdate:update)",
        "  StartActivity:tracing_activity",
        "    RunActivity:tracing_activity",
        "    RunActivity:tracing_activity",
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
                        if link_span.context.span_id == link.context.span_id:
                            span_links.append(link_span.name)
                span_str += f" (links: {', '.join(span_links)})"
            # Signals can duplicate in rare situations, so we make sure not to
            # re-add
            if "Signal" in span_str and span_str in ret:
                continue
            ret.append(span_str)
            ret += dump_spans(
                spans,
                parent_id=span.context.span_id,
                with_attributes=with_attributes,
                indent_depth=indent_depth + 1,
            )
    return ret


# TODO(cretz): Additional tests to write
# * query without interceptor (no headers)
# * workflow without interceptor (no headers) but query with interceptor (headers)
# * workflow failure and wft failure
# * signal with start
# * signal failure and wft failure from signal
