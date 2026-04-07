"""
See sibling file test_workflow_caller_cancellation_types.py for explanatory comments.
"""

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
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.nexus.test_workflow_caller_cancellation_types import (
    assert_event_subsequence,
    get_event_time,
    has_event,
)


@dataclass
class TestContext:
    __test__ = False
    cancellation_type: workflow.NexusOperationCancellationType
    cancel_handler_released: asyncio.Future[datetime] = field(
        default_factory=asyncio.Future
    )


test_context: TestContext


@workflow.defn(sandboxed=False)
class HandlerWorkflow:
    def __init__(self):
        self.cancel_handler_released = asyncio.Event()
        self.caller_op_future_resolved = asyncio.Event()

    @workflow.run
    async def run(self) -> None:
        # We want the cancel handler to be invoked, so this workflow must not close before
        # then.
        await self.cancel_handler_released.wait()
        if (
            test_context.cancellation_type
            == workflow.NexusOperationCancellationType.WAIT_REQUESTED
        ):
            # For WAIT_REQUESTED, we want to prove that the future can be unblocked before the
            # handler workflow completes.
            await self.caller_op_future_resolved.wait()

    @workflow.signal
    def set_cancel_handler_released(self) -> None:
        self.cancel_handler_released.set()

    @workflow.signal
    def set_caller_op_future_resolved(self) -> None:
        self.caller_op_future_resolved.set()


@nexusrpc.service
class Service:
    workflow_op: nexusrpc.Operation[None, None]


class WorkflowOpHandler(
    temporalio.nexus._operation_handlers.WorkflowRunOperationHandler
):
    def __init__(self):  # type:ignore[reportMissingSuperCall]
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
        client = nexus.client()
        handler_wf: WorkflowHandle[HandlerWorkflow, None] = (
            client.get_workflow_handle_for(
                HandlerWorkflow.run,
                workflow_id=nexus.WorkflowHandle[None].from_token(token).workflow_id,
            )
        )
        await handler_wf.signal(HandlerWorkflow.set_cancel_handler_released)
        test_context.cancel_handler_released.set_result(datetime.now(timezone.utc))
        raise nexusrpc.HandlerError(
            "Deliberate non-retryable error in cancel handler",
            type=nexusrpc.HandlerErrorType.BAD_REQUEST,
        )


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
    error_type: str | None = None
    error_cause_type: str | None = None


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
        error_type, error_cause_type = None, None
        try:
            await op_handle
        except exceptions.NexusOperationError as err:
            error_type = err.__class__.__name__
            error_cause_type = err.__cause__.__class__.__name__

        self.caller_op_future_resolved.set_result(workflow.now())
        assert op_handle.operation_token
        await workflow.wait_condition(lambda: self.released)
        return CancellationResult(
            operation_token=op_handle.operation_token,
            error_type=error_type,
            error_cause_type=error_cause_type,
            caller_op_future_resolved=self.caller_op_future_resolved.result(),
        )


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
    test_context = TestContext(cancellation_type=cancellation_type)

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
            id="caller-wf-" + str(uuid.uuid4()),
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
    result = await caller_wf.result()
    assert result.error_type == "NexusOperationError"
    assert result.error_cause_type == "CancelledError"

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
    await handler_wf.result()
    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()
    assert result.error_type == "NexusOperationError"
    assert result.error_cause_type == "CancelledError"

    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_FAILED,
        ],
    )
    op_cancel_requested_event = await get_event_time(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
    )
    op_cancel_request_failed_event = await get_event_time(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_FAILED,
    )
    assert (
        result.caller_op_future_resolved
        <= op_cancel_requested_event
        <= op_cancel_request_failed_event
    )


async def check_behavior_for_wait_cancellation_requested(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle,
) -> None:
    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()
    assert result.error_type == "NexusOperationError"
    assert result.error_cause_type == "HandlerError"
    await handler_wf.signal(HandlerWorkflow.set_caller_op_future_resolved)
    await handler_wf.result()
    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_FAILED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        ],
    )
    op_cancel_request_failed = await get_event_time(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_FAILED,
    )
    handler_wf_completed = await get_event_time(
        handler_wf,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
    )
    assert (
        op_cancel_request_failed
        <= result.caller_op_future_resolved
        <= handler_wf_completed
    )


async def check_behavior_for_wait_cancellation_completed(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle,
) -> None:
    await handler_wf.result()
    await caller_wf.signal(CallerWorkflow.release)
    result = await caller_wf.result()
    assert not result.error_type
    # Note that the relative order of these two events is non-deterministic, since one is the result
    # of the cancel handler response being processed and the other is the result of the handler
    # workflow exiting.
    # (caller_wf, EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_FAILED)
    # (handler_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED)
    await assert_event_subsequence(
        caller_wf,
        [
            EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
            EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
        ],
    )
    handler_wf_completed = await get_event_time(
        handler_wf,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
    )
    assert handler_wf_completed <= result.caller_op_future_resolved
