"""End-to-end (server-based) tests for Nexus signal-backlink propagation.

These exercise, against a real server, the bidirectional
link propagation that occurs when a Nexus operation handler signals (or signal-with-starts) a
workflow:

- Forward: the caller's ``NexusOperationScheduled`` event is referenced by the callee's
  ``WorkflowExecutionSignaled`` event (attached to the signal RPC the handler issues).
- Backward: a backlink pointing at the callee's ``WorkflowExecutionSignaled`` event lands on
  the caller's ``NexusOperationCompleted`` event (sync handler) or ``NexusOperationStarted``
  event (async handler).

The backward direction is produced server-side (temporalio/temporal#9897) and is gated by
``history.enableCHASMSignalBacklinks=true`` (added to the local dev-server args in
``tests/conftest.py``). The server populates the backlink's reference via ``RequestIdReference``
rather than ``EventReference``, so backlink assertions tolerate both oneof variants of
``common.v1.Link.WorkflowEvent.reference`` (see ``_backlink_event_type``). When run against a
server that does not emit the backlink, the backward assertions are skipped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from nexusrpc import Operation, service
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
    sync_operation,
)
from nexusrpc.handler._decorators import operation_handler

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.history.v1
from temporalio import nexus, workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name

EventType = temporalio.api.enums.v1.EventType


# ── Service definition ──────────────────────────────────────────────────────────────────────


@dataclass
class OpInput:
    mode: str
    callee_id: str


@service
class SignalingService:
    op: Operation[OpInput, str]


# ── Callee workflow ───────────────────────────────────────────────────────────────────────


@workflow.defn
class CalleeWorkflow:
    def __init__(self) -> None:
        self._received: list[str] = []
        self._expected = 1

    @workflow.run
    async def run(self, expected_signals: int) -> str:
        self._expected = expected_signals
        await workflow.wait_condition(lambda: len(self._received) >= self._expected)
        return ",".join(self._received)

    @workflow.signal
    def ping(self, msg: str) -> None:
        self._received.append(msg)


# ── Caller workflow ───────────────────────────────────────────────────────────────────────


@workflow.defn
class CallerWorkflow:
    @workflow.run
    async def run(self, mode: str, callee_id: str, task_queue: str) -> str:
        client = workflow.create_nexus_client(
            service=SignalingService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await client.execute_operation(
            SignalingService.op, OpInput(mode=mode, callee_id=callee_id)
        )


# ── Nexus service handler ─────────────────────────────────────────────────────────────────

MODE_SYNC = "sync"
MODE_ASYNC = "async"


class _AsyncSignalingOperation(OperationHandler[OpInput, str]):
    """Signal-with-starts the callee then returns an async result.

    The backlink stashed on ``ctx.outbound_links`` by the signal-with-start RPC is carried on
    the start-operation response, landing on the caller's ``NexusOperationStarted`` event.
    """

    async def start(
        self, ctx: StartOperationContext, input: OpInput
    ) -> StartOperationResultAsync:
        await _signal_with_start(input.callee_id, "async-signal")
        return StartOperationResultAsync(token=f"async-op-{uuid.uuid4()}")

    async def cancel(self, ctx, token: str) -> None:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def fetch_info(self, ctx, token: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def fetch_result(self, ctx, token: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@service_handler(service=SignalingService)
class SignalingServiceHandler:
    @sync_operation
    async def op(self, _ctx: StartOperationContext, input: OpInput) -> str:
        # Synchronous path: signal-with-start the callee (first signal) then plain-signal it
        # (second signal). Both backlinks are carried on the sync start-operation response and
        # land on the caller's NexusOperationCompleted event.
        await _signal_with_start(input.callee_id, "first")
        await (
            nexus.client()
            .get_workflow_handle(input.callee_id)
            .signal(CalleeWorkflow.ping, "second")
        )
        return "ok:sync"


# A separate service exposing only the async operation, so the caller can address it by name.
@service
class AsyncSignalingService:
    op: Operation[OpInput, str]


@service_handler(service=AsyncSignalingService)
class AsyncSignalingServiceHandler:
    @operation_handler
    def op(self) -> OperationHandler[OpInput, str]:
        return _AsyncSignalingOperation()


async def _signal_with_start(callee_id: str, payload: str) -> None:
    # signal-with-start exercises the SignalWithStartWorkflowExecutionResponse.signal_link
    # backlink path in temporalio.client._impl.
    await nexus.client().start_workflow(
        CalleeWorkflow.run,
        2 if payload == "first" else 1,
        id=callee_id,
        task_queue=nexus.info().task_queue,
        start_signal="ping",
        start_signal_args=[payload],
    )


@workflow.defn
class AsyncSignalCallerWorkflow:
    @workflow.run
    async def run(self, callee_id: str, task_queue: str) -> str:
        client = workflow.create_nexus_client(
            service=AsyncSignalingService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        handle = await client.start_operation(
            AsyncSignalingService.op, OpInput(mode=MODE_ASYNC, callee_id=callee_id)
        )
        # Do not await the result: the async op never completes (no completion is delivered).
        # Returning the token confirms the operation reached the Started state, whose history
        # event carries the backlink.
        return handle.operation_token or "async-started"


# ── Assertion helpers ───────────────────────────────────────────────────────────────────────


def _events_of_type(
    history: WorkflowHistory,
    event_type: temporalio.api.enums.v1.EventType.ValueType,
) -> list[temporalio.api.history.v1.HistoryEvent]:
    return [e for e in history.events if e.event_type == event_type]


def _backlink_event_type(
    we: temporalio.api.common.v1.Link.WorkflowEvent,
) -> temporalio.api.enums.v1.EventType.ValueType:
    # Server PR #9897 keys backlinks via RequestIdReference rather than EventReference; accept
    # either oneof variant (matches Java SignalOperationLinkingTest.assertBacklink).
    if we.HasField("request_id_ref"):
        return we.request_id_ref.event_type
    return we.event_ref.event_type


def _assert_forward_link(
    callee_history: WorkflowHistory,
    caller_id: str,
    expected_count: int,
) -> None:
    signaled = _events_of_type(
        callee_history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    )
    assert len(signaled) == expected_count, (
        f"expected {expected_count} WorkflowExecutionSignaled events, got {len(signaled)}"
    )
    for event in signaled:
        assert len(event.links) >= 1, (
            "expected at least one link on each WorkflowExecutionSignaled event"
        )
        we = event.links[0].workflow_event
        assert we.workflow_id == caller_id, (
            "forward link should reference the caller workflow"
        )
        assert we.event_ref.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED


def _assert_backlink(
    event: temporalio.api.history.v1.HistoryEvent, callee_id: str
) -> bool:
    """Assert the event carries a signal-event backlink to the callee.

    Returns False (and asserts nothing) if no backlink is present, so the test soft-passes
    against a server that does not emit backlinks.
    """
    if len(event.links) < 1:
        return False
    we = event.links[0].workflow_event
    assert we.workflow_id == callee_id, "backlink should reference the callee workflow"
    assert _backlink_event_type(we) == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    return True


# ── Tests ─────────────────────────────────────────────────────────────────────────────────


async def test_sync_signal_operation_links(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    callee_id = f"callee-{uuid.uuid4()}"
    caller_id = f"caller-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[SignalingServiceHandler()],
        workflows=[CallerWorkflow, CalleeWorkflow],
    ):
        caller_handle = await client.start_workflow(
            CallerWorkflow.run,
            args=[MODE_SYNC, callee_id, task_queue],
            id=caller_id,
            task_queue=task_queue,
        )
        assert await caller_handle.result() == "ok:sync"

        callee_result = await client.get_workflow_handle(callee_id).result()
        assert callee_result == "first,second"

        caller_history = await caller_handle.fetch_history()
        callee_history = await client.get_workflow_handle(callee_id).fetch_history()

    # Forward: both signal events on the callee reference the caller's scheduled event.
    _assert_forward_link(callee_history, caller_id, expected_count=2)

    # Backward: the single NexusOperationCompleted carries backlinks to the callee.
    completed = _events_of_type(
        caller_history, EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED
    )
    assert len(completed) == 1, (
        f"expected exactly one NexusOperationCompleted event, got {len(completed)}"
    )
    if not _assert_backlink(completed[0], callee_id):
        pytest.skip(
            "server did not emit a signal backlink "
            "(history.enableCHASMSignalBacklinks not enabled)"
        )


async def test_async_signal_operation_links(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    callee_id = f"async-callee-{uuid.uuid4()}"
    caller_id = f"async-caller-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[AsyncSignalingServiceHandler()],
        workflows=[AsyncSignalCallerWorkflow, CalleeWorkflow],
    ):
        caller_handle = await client.start_workflow(
            AsyncSignalCallerWorkflow.run,
            args=[callee_id, task_queue],
            id=caller_id,
            task_queue=task_queue,
        )
        # Caller returns once the async operation reaches Started; result is the op token.
        assert await caller_handle.result()

        callee_result = await client.get_workflow_handle(callee_id).result()
        assert callee_result == "async-signal"

        caller_history = await caller_handle.fetch_history()
        callee_history = await client.get_workflow_handle(callee_id).fetch_history()

    # Forward: the single signal event on the callee references the caller's scheduled event.
    _assert_forward_link(callee_history, caller_id, expected_count=1)

    # Backward: the backlink lands on NexusOperationStarted for the async response path.
    started = _events_of_type(
        caller_history, EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED
    )
    assert len(started) == 1, (
        f"expected exactly one NexusOperationStarted event, got {len(started)}"
    )
    if not _assert_backlink(started[0], callee_id):
        pytest.skip(
            "server did not emit a signal backlink "
            "(history.enableCHASMSignalBacklinks not enabled)"
        )
