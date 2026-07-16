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
``common.v1.Link.WorkflowEvent.reference`` (see ``workflow_event_link_event_type``). When run
against a server that does not emit the backlink, the backward assertions are skipped.

The forward/backward description above applies to operations scheduled by a caller workflow. The
file also covers the same handlers invoked as standalone (client-initiated) operations via
``client.create_nexus_client``; that case has no caller workflow, so the forward link is a
``NexusOperation`` link to the operation execution itself and only the forward direction is
asserted (see the standalone tests near the end of the file for details).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

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

import temporalio.api.enums.v1
import temporalio.api.history.v1
import temporalio.common
from temporalio import nexus, workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually
from tests.helpers.nexus import (
    events_of_type,
    make_nexus_endpoint_name,
    workflow_event_link_event_type,
)

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


# A service whose handler issues a plain start_workflow (no signal, not the nexus-backing
# workflow) against an already-running callee under a USE_EXISTING conflict policy.
@service
class StartConflictService:
    op: Operation[OpInput, str]


@service_handler(service=StartConflictService)
class StartConflictServiceHandler:
    @sync_operation
    async def op(self, _ctx: StartOperationContext, input: OpInput) -> str:
        # Plain start_workflow from inside a Nexus operation handler, targeting the
        # already-running callee with WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING. The build path
        # enables on_conflict_options, so the detected conflict attaches this request's request
        # id (and links) to the existing run.
        await nexus.client().start_workflow(
            CalleeWorkflow.run,
            1,
            id=input.callee_id,
            task_queue=nexus.info().task_queue,
            id_conflict_policy=temporalio.common.WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return "ok:conflict"


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


def _assert_forward_link(
    callee_history: WorkflowHistory,
    caller_id: str,
    expected_count: int,
) -> None:
    signaled = events_of_type(
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
    assert (
        workflow_event_link_event_type(we)
        == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    )
    return True


# ── Tests ─────────────────────────────────────────────────────────────────────────────────


async def test_sync_signal_operation_links(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping_v1:
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
    completed = events_of_type(
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
    if env.supports_time_skipping_v1:
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
    started = events_of_type(
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


# ── Standalone (client-initiated) operations ─────────────────────────────────────────────────
#
# The tests above drive the operation from a caller workflow. The tests below invoke the same
# handlers directly via ``client.create_nexus_client`` (a standalone Nexus operation, with no
# caller workflow). The forward link still propagates, but as a ``NexusOperation`` link
# referencing the operation execution itself rather than a ``WorkflowEvent`` link to a caller's
# ``NexusOperationScheduled`` event. The backward (response) link lands on the standalone
# operation's own execution, which is a CHASM ``nexusoperation.operation`` archetype and is not
# retrievable via the workflow history API, so only the forward direction is asserted here.


def _assert_standalone_forward_link(
    callee_history: WorkflowHistory,
    operation_id: str,
    expected_count: int,
) -> None:
    signaled = events_of_type(
        callee_history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    )
    assert len(signaled) == expected_count, (
        f"expected {expected_count} WorkflowExecutionSignaled events, got {len(signaled)}"
    )
    for event in signaled:
        assert len(event.links) >= 1, (
            "expected at least one link on each WorkflowExecutionSignaled event"
        )
        link = event.links[0]
        assert link.HasField("nexus_operation"), (
            "standalone forward link should be a NexusOperation link"
        )
        assert link.nexus_operation.operation_id == operation_id, (
            "forward link should reference the standalone Nexus operation"
        )


async def test_standalone_sync_signal_operation_links(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    callee_id = f"standalone-callee-{uuid.uuid4()}"
    operation_id = f"standalone-op-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[SignalingServiceHandler()],
        workflows=[CalleeWorkflow],
    ):
        nexus_client = client.create_nexus_client(
            service=SignalingService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        handle = await nexus_client.start_operation(
            SignalingService.op,
            OpInput(mode=MODE_SYNC, callee_id=callee_id),
            id=operation_id,
            schedule_to_close_timeout=timedelta(seconds=20),
        )
        assert await handle.result() == "ok:sync"

        callee_result = await client.get_workflow_handle(callee_id).result()
        assert callee_result == "first,second"

        callee_history = await client.get_workflow_handle(callee_id).fetch_history()

    # Forward: both signal events on the callee reference the standalone Nexus operation.
    _assert_standalone_forward_link(callee_history, operation_id, expected_count=2)


async def test_standalone_async_signal_operation_links(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    callee_id = f"standalone-async-callee-{uuid.uuid4()}"
    operation_id = f"standalone-async-op-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[AsyncSignalingServiceHandler()],
        workflows=[CalleeWorkflow],
    ):
        nexus_client = client.create_nexus_client(
            service=AsyncSignalingService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        # The async operation never completes (no completion is delivered), so we do not await
        # its result; the handler signals the callee during start.
        await nexus_client.start_operation(
            AsyncSignalingService.op,
            OpInput(mode=MODE_ASYNC, callee_id=callee_id),
            id=operation_id,
            schedule_to_close_timeout=timedelta(seconds=20),
        )

        async def _callee_result() -> str:
            # The callee is signal-with-started from the handler; tolerate the brief window
            # before it exists.
            try:
                return await client.get_workflow_handle(callee_id).result()
            except RPCError as err:
                if err.status == RPCStatusCode.NOT_FOUND:
                    raise AssertionError("callee not created yet")
                raise

        assert await assert_eventually(_callee_result) == "async-signal"

        callee_history = await client.get_workflow_handle(callee_id).fetch_history()

    # Forward: the single signal event on the callee references the standalone operation.
    _assert_standalone_forward_link(callee_history, operation_id, expected_count=1)


# ── on-conflict options for a plain start from a handler ──────────────────────────────────────
#
# When a Nexus operation handler issues a plain start_workflow (no signal, not the nexus-backing
# workflow) against an already-running workflow under a USE_EXISTING conflict policy, the SDK
# enables on_conflict_options so the server attaches this request's request id (and links) to the
# existing run. The attachment surfaces as a WorkflowExecutionOptionsUpdated event on the
# existing workflow's history. Older servers may not emit it, so the assertion soft-skips.


async def test_start_from_handler_attaches_on_conflict_options(
    client: Client,
    env: WorkflowEnvironment,
) -> None:
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    callee_id = f"conflict-callee-{uuid.uuid4()}"
    operation_id = f"conflict-op-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StartConflictServiceHandler()],
        workflows=[CalleeWorkflow],
    ):
        # Start the callee first so the handler's start_workflow hits a conflict.
        callee_handle = await client.start_workflow(
            CalleeWorkflow.run,
            1,
            id=callee_id,
            task_queue=task_queue,
        )

        nexus_client = client.create_nexus_client(
            service=StartConflictService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        handle = await nexus_client.start_operation(
            StartConflictService.op,
            OpInput(mode=MODE_SYNC, callee_id=callee_id),
            id=operation_id,
            schedule_to_close_timeout=timedelta(seconds=20),
        )
        # USE_EXISTING resolves the conflict to the existing run; the operation succeeds.
        assert await handle.result() == "ok:conflict"

        # Release the callee so it terminates cleanly, then read its history.
        await callee_handle.signal(CalleeWorkflow.ping, "done")
        assert await callee_handle.result() == "done"
        callee_history = await callee_handle.fetch_history()

    updated = events_of_type(
        callee_history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_OPTIONS_UPDATED
    )
    if not updated:
        pytest.skip(
            "server did not emit a WorkflowExecutionOptionsUpdated event "
            "(on_conflict_options not honored by this server version)"
        )
    # The conflict resolution attached this start request's request id to the existing run,
    # which only happens because on_conflict_options.attach_request_id was set on the request.
    assert any(
        e.workflow_execution_options_updated_event_attributes.attached_request_id
        for e in updated
    ), (
        "expected a WorkflowExecutionOptionsUpdated event carrying an attached request id"
    )
