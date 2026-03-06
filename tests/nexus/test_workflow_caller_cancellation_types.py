import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import nexusrpc
import nexusrpc.handler._decorators
import pytest

import temporalio.nexus._operation_handlers
from temporalio import exceptions, nexus, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import (
    WithStartWorkflowOperation,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class TestContext:
    __test__ = False
    cancellation_type: workflow.NexusOperationCancellationType
    caller_workflow_id: str
    cancel_handler_released: asyncio.Future[datetime] = field(
        default_factory=asyncio.Future
    )


test_context: TestContext


@workflow.defn(sandboxed=False)
class HandlerWorkflow:
    def __init__(self):
        self.caller_op_future_resolved = asyncio.Event()

    @workflow.run
    async def run(self) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if test_context.cancellation_type in [
                workflow.NexusOperationCancellationType.TRY_CANCEL,
                workflow.NexusOperationCancellationType.WAIT_REQUESTED,
            ]:
                # We want to prove that the caller op future can be resolved before the operation
                # (i.e. its backing workflow) is cancelled.
                await self.caller_op_future_resolved.wait()
            raise

    @workflow.signal
    def set_caller_op_future_resolved(self) -> None:
        self.caller_op_future_resolved.set()


@nexusrpc.service
class Service:
    workflow_op: nexusrpc.Operation[None, None]


class WorkflowOpHandler(
    temporalio.nexus._operation_handlers.WorkflowRunOperationHandler
):
    def __init__(self):  #  type:ignore[reportMissingSuperCall]
        pass

    async def start(
        self, ctx: nexusrpc.handler.StartOperationContext, input: None
    ) -> nexusrpc.handler.StartOperationResultAsync:
        tctx = nexus.WorkflowRunOperationContext._from_start_operation_context(ctx)
        handle = await tctx.start_workflow(
            HandlerWorkflow.run,
            id="handler-wf-" + str(uuid.uuid4()),
        )
        return nexusrpc.handler.StartOperationResultAsync(token=handle.to_token())

    async def cancel(
        self, ctx: nexusrpc.handler.CancelOperationContext, token: str
    ) -> None:
        if (
            test_context.cancellation_type
            == workflow.NexusOperationCancellationType.TRY_CANCEL
        ):
            # When this cancel handler returns, a nexus task completion will be sent to the handler
            # server, and the handler server will respond to the nexus cancel request that was made
            # by the caller server. At that point, the caller server will write
            # NexusOperationCancelRequestCompleted. For TRY_CANCEL we want to prove that the nexus
            # op handle future can be resolved as cancelled before any of that.
            caller_wf: WorkflowHandle[Any, CancellationResult] = (
                nexus.client().get_workflow_handle_for(
                    CallerWorkflow.run,
                    workflow_id=test_context.caller_workflow_id,
                )
            )
            await caller_wf.execute_update(
                CallerWorkflow.wait_caller_op_future_resolved
            )
        test_context.cancel_handler_released.set_result(datetime.now(timezone.utc))
        await super().cancel(ctx, token)


@nexusrpc.handler.service_handler(service=Service)
class ServiceHandler:
    @nexusrpc.handler._decorators.operation_handler
    def workflow_op(self) -> nexusrpc.handler.OperationHandler[None, None]:
        return WorkflowOpHandler()


@dataclass
class Input:
    endpoint: str
    cancellation_type: workflow.NexusOperationCancellationType | None


@dataclass
class CancellationResult:
    operation_token: str
    caller_op_future_resolved: datetime


@workflow.defn(sandboxed=False)
class CallerWorkflow:
    @workflow.init
    def __init__(self, input: Input):
        self.nexus_client = workflow.create_nexus_client(
            service=Service,
            endpoint=input.endpoint,
        )
        self.released = False
        self.operation_token: str | None = None
        self.caller_op_future_resolved: asyncio.Future[datetime] = asyncio.Future()

    @workflow.signal
    def release(self):
        self.released = True

    @workflow.update
    async def get_operation_token(self) -> str:
        await workflow.wait_condition(lambda: self.operation_token is not None)
        assert self.operation_token
        return self.operation_token

    @workflow.update
    async def wait_caller_op_future_resolved(self) -> None:
        await self.caller_op_future_resolved

    @workflow.run
    async def run(self, input: Input) -> CancellationResult:
        op_handle = await (
            self.nexus_client.start_operation(
                Service.workflow_op,
                input=None,
                cancellation_type=input.cancellation_type,
            )
            if input.cancellation_type is not None
            else self.nexus_client.start_operation(Service.workflow_op, input=None)
        )
        self.operation_token = op_handle.operation_token
        assert self.operation_token
        # Request cancellation of the asyncio task representing the nexus operation. When the handle
        # task is awaited, the resulting asyncio.CancelledError is caught, and a
        # RequestCancelNexusOperation command is emitted instead (see
        # _WorkflowInstanceImpl._outbound_start_nexus_operation).
        #
        # On processing this command in the activation completion, the sdk-core nexus_operation
        # state machine transitions behaves as follows for the different cancellation types:
        #
        # - Abandon and TryCancel: Immediately resolves with cancellation (i.e. via a second
        #                          activation in the same WFT)
        #
        # For non-Abandon types, a RequestCancelNexusOperation command is sent to the server:
        #
        # - TryCancel: Immediately resolve the handle task as cancelled, but also cause the server
        #              to write NexusOperationCancelRequested.
        #
        # - WaitCancellationRequested: waits for NexusOperationCancelRequestCompleted (i.e. nexus op
        #              cancel handler has responded) before sending an activation job to Python
        #              resolving the nexus operation as cancelled
        # - WaitCancellationCompleted: waits for NexusOperationCanceled (e.g. backing workflow has
        #              closed as cancelled) before sending an activation job to Python resolving the
        #              nexus operation as cancelled
        op_handle.cancel()
        if (
            test_context.cancellation_type
            == workflow.NexusOperationCancellationType.WAIT_REQUESTED
        ):
            # For WAIT_REQUESTED, we need core to receive the NexusOperationCancelRequestCompleted
            # event. That event should trigger a workflow task, but does not currently due to
            # https://github.com/temporalio/temporal/issues/8175. Force a new WFT, allowing time for
            # the event hopefully to arrive.
            await workflow.sleep(0.1, summary="Force new WFT")
        try:
            await op_handle
        except exceptions.NexusOperationError:
            self.caller_op_future_resolved.set_result(workflow.now())
            assert op_handle.operation_token
            if input.cancellation_type in [
                workflow.NexusOperationCancellationType.TRY_CANCEL,
                workflow.NexusOperationCancellationType.WAIT_REQUESTED,
            ]:
                # We want to prove that the future can be unblocked before the handler workflow is
                # cancelled. Send a signal, so that handler workflow can wait for it.
                await workflow.get_external_workflow_handle_for(
                    HandlerWorkflow.run,
                    workflow_id=(
                        nexus.WorkflowHandle[None]
                        .from_token(self.operation_token)
                        .workflow_id
                    ),
                ).signal(HandlerWorkflow.set_caller_op_future_resolved)

            await workflow.wait_condition(lambda: self.released)
            return CancellationResult(
                operation_token=op_handle.operation_token,
                caller_op_future_resolved=self.caller_op_future_resolved.result(),
            )
        else:
            pytest.fail("Expected NexusOperationError")


@pytest.mark.parametrize(
    "cancellation_type_name",
    [
        workflow.NexusOperationCancellationType.ABANDON.name,
        workflow.NexusOperationCancellationType.TRY_CANCEL.name,
        workflow.NexusOperationCancellationType.WAIT_REQUESTED.name,
        workflow.NexusOperationCancellationType.WAIT_COMPLETED.name,
    ],
)
async def test_cancellation_type(
    env: WorkflowEnvironment,
    cancellation_type_name: str,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    cancellation_type = workflow.NexusOperationCancellationType[cancellation_type_name]
    global test_context
    test_context = TestContext(
        cancellation_type=cancellation_type,
        caller_workflow_id="caller-wf-" + str(uuid.uuid4()),
    )

    client = env.client

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[CallerWorkflow, HandlerWorkflow],
        nexus_service_handlers=[ServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(worker.task_queue), worker.task_queue
        )

        # Start the caller workflow, wait for the nexus op to have started and retrieve the nexus op
        # token
        with_start_workflow = WithStartWorkflowOperation(
            CallerWorkflow.run,
            Input(
                endpoint=make_nexus_endpoint_name(worker.task_queue),
                cancellation_type=cancellation_type,
            ),
            id=test_context.caller_workflow_id,
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )

        operation_token = await client.execute_update_with_start_workflow(
            CallerWorkflow.get_operation_token,
            start_workflow_operation=with_start_workflow,
        )
        handler_wf = (
            nexus.WorkflowHandle[None]
            .from_token(operation_token)
            ._to_client_workflow_handle(client)
        )
        caller_wf = await with_start_workflow.workflow_handle()

        if cancellation_type == workflow.NexusOperationCancellationType.ABANDON:
            await check_behavior_for_abandon(caller_wf, handler_wf)
        elif cancellation_type == workflow.NexusOperationCancellationType.TRY_CANCEL:
            await check_behavior_for_try_cancel(caller_wf, handler_wf)
        elif (
            cancellation_type == workflow.NexusOperationCancellationType.WAIT_REQUESTED
        ):
            await check_behavior_for_wait_cancellation_requested(caller_wf, handler_wf)
        elif (
            cancellation_type == workflow.NexusOperationCancellationType.WAIT_COMPLETED
        ):
            await check_behavior_for_wait_cancellation_completed(caller_wf, handler_wf)
        else:
            pytest.fail(f"Invalid cancellation type: {cancellation_type}")


async def check_behavior_for_abandon(
    caller_wf: WorkflowHandle,
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is not sent.
    """
    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.RUNNING
    await caller_wf.signal(CallerWorkflow.release)
    await caller_wf.result()
    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        ],
    )
    assert not await has_event(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
    )


async def check_behavior_for_try_cancel(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle[Any, None],
) -> None:
    """
    Check that a cancellation request is sent and the caller workflow nexus op future is unblocked
    as cancelled before the caller server writes CANCEL_REQUESTED.

    There is a race between (a) the caller server writing CANCEL_REQUEST_COMPLETED in response to
    the cancel handler returning, and (b) the caller server writing CANCELED in response to the
    handler workflow exiting as canceled. If (b) happens first then (a) may never happen, therefore
    we do not make any assertions regarding CANCEL_REQUEST_COMPLETED.
    """
    try:
        await handler_wf.result()
    except WorkflowFailureError as err:
        assert isinstance(err.__cause__, exceptions.CancelledError)
    else:
        pytest.fail("Expected WorkflowFailureError")
    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()

    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.CANCELED
    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
        ],
    )
    op_cancel_requested_event = await get_event_time(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
    )
    assert result.caller_op_future_resolved <= op_cancel_requested_event


async def check_behavior_for_wait_cancellation_requested(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is sent and the caller workflow nexus operation future is
    unblocked as cancelled after the cancel handler returns (i.e. after the
    NexusOperationCancelRequestCompleted in the caller workflow history) but without waiting for
    the operation to be canceled.
    """
    try:
        await handler_wf.result()
    except WorkflowFailureError as err:
        assert isinstance(err.__cause__, exceptions.CancelledError)
    else:
        pytest.fail("Expected WorkflowFailureError")

    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()

    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.CANCELED
    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_COMPLETED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
        ],
    )
    op_cancel_request_completed = await get_event_time(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_COMPLETED,
    )
    op_canceled = await get_event_time(
        handler_wf,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
    )
    assert op_cancel_request_completed <= result.caller_op_future_resolved < op_canceled


async def check_behavior_for_wait_cancellation_completed(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is sent and the caller workflow nexus operation future is
    unblocked after the operation is canceled.
    """
    try:
        await handler_wf.result()
    except WorkflowFailureError as err:
        assert isinstance(err.__cause__, exceptions.CancelledError)
    else:
        pytest.fail("Expected WorkflowFailureError")

    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.CANCELED

    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()

    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        ],
    )
    handler_wf_canceled_event = await get_event_time(
        handler_wf,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
    )
    assert handler_wf_canceled_event <= result.caller_op_future_resolved, (
        "expected caller op future resolved after handler workflow canceled, but got "
        f"{result.caller_op_future_resolved} before {handler_wf_canceled_event}"
    )


async def has_event(wf_handle: WorkflowHandle, event_type: EventType.ValueType):
    async for e in wf_handle.fetch_history_events():
        if e.event_type == event_type:
            return True
    return False


async def get_event_time(
    wf_handle: WorkflowHandle,
    event_type: EventType.ValueType,
) -> datetime:
    async for event in wf_handle.fetch_history_events():
        if event.event_type == event_type:
            return event.event_time.ToDatetime().replace(tzinfo=timezone.utc)
    event_type_name = EventType.Name(event_type).removeprefix("EVENT_TYPE_")
    assert False, f"Event {event_type_name} not found in {wf_handle.id}"


async def assert_event_subsequence(
    wf_handle: WorkflowHandle,
    expected_events: list[EventType.ValueType],
) -> None:
    """
    Given a workflow handle and a sequence of event types, assert that the workflow's history
    contains that subsequence of events in the order specified.
    """
    all_events = []
    async for e in wf_handle.fetch_history_events():
        all_events.append(e)

    _all_events = iter(all_events)
    _expected_events = iter(expected_events)

    previous_expected_event_type_name = None
    for expected_event_type in _expected_events:
        expected_event_type_name = EventType.Name(expected_event_type).removeprefix(
            "EVENT_TYPE_"
        )
        has_expected = next(
            (e for e in _all_events if e.event_type == expected_event_type),
            None,
        )
        if not has_expected:
            if previous_expected_event_type_name is not None:
                prefix = f"After {previous_expected_event_type_name}, "
            else:
                prefix = ""
            pytest.fail(
                f"{prefix}expected {expected_event_type_name} in workflow {wf_handle.id}"
            )
        previous_expected_event_type_name = expected_event_type_name
