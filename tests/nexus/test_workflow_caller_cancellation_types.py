import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import nexusrpc
import pytest

from temporalio import exceptions, nexus, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowHandle,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@workflow.defn
class HandlerWorkflow:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Future()


@nexusrpc.service
class Service:
    workflow_op: nexusrpc.Operation[None, None]


@nexusrpc.handler.service_handler(service=Service)
class ServiceHandler:
    @nexus.workflow_run_operation
    async def workflow_op(
        self, ctx: nexus.WorkflowRunOperationContext, _input: None
    ) -> nexus.WorkflowHandle[None]:
        return await ctx.start_workflow(
            HandlerWorkflow.run,
            id="handler-wf-" + str(uuid.uuid4()),
        )


@dataclass
class Input:
    endpoint: str
    cancellation_type: Optional[workflow.NexusOperationCancellationType]


@dataclass
class CancellationResult:
    operation_token: str
    caller_unblock_time: datetime


@workflow.defn
class CallerWorkflow:
    @workflow.init
    def __init__(self, input: Input):
        self.nexus_client = workflow.create_nexus_client(
            service=Service,
            endpoint=input.endpoint,
        )

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
        op_handle.cancel()
        try:
            await op_handle
        except exceptions.NexusOperationError:
            caller_unblock_time = workflow.now()
            # Give time for handler wf cancellation event to be written if a request was sent
            await asyncio.sleep(0.5)
            assert op_handle.operation_token
            return CancellationResult(
                operation_token=op_handle.operation_token,
                caller_unblock_time=caller_unblock_time,
            )
        else:
            pytest.fail("Expected NexusOperationError")


async def check_behavior_for_abandon(
    caller_wf: WorkflowHandle,
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is not sent.
    """
    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.RUNNING
    await _assert_event_subsequence(
        [
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED),
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED),
        ]
    )
    assert not await _has_event(
        caller_wf,
        EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
    )


async def check_behavior_for_try_cancel(
    caller_wf: WorkflowHandle,
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is sent and the caller workflow exits before the operation is
    canceled.
    """
    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.CANCELED
    await _assert_event_subsequence(
        [
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED),
            (caller_wf, EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED),
            (
                handler_wf,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED,
            ),
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED),
        ]
    )


async def check_behavior_for_wait_cancellation_completed(
    caller_wf: WorkflowHandle[Any, CancellationResult],
    handler_wf: WorkflowHandle,
) -> None:
    """
    Check that a cancellation request is sent and the caller workflow nexus operation future is
    unblocked after the operation is canceled.
    """
    handler_status = (await handler_wf.describe()).status
    assert handler_status == WorkflowExecutionStatus.CANCELED
    await _assert_event_subsequence(
        [
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED),
            (caller_wf, EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED),
            (
                handler_wf,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED,
            ),
            (handler_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED),
            (caller_wf, EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED),
            (caller_wf, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED),
        ]
    )
    result = await caller_wf.result()
    handler_wf_canceled_event_time = await _get_event_time(
        handler_wf,
        EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
    )
    assert (
        result.caller_unblock_time > handler_wf_canceled_event_time
    ), "For WAIT_COMPLETED, the future should be unblocked after handler workflow cancellation. "


@pytest.mark.parametrize(
    "cancellation_type",
    [
        None,
        workflow.NexusOperationCancellationType.ABANDON.name,
        workflow.NexusOperationCancellationType.TRY_CANCEL.name,
        workflow.NexusOperationCancellationType.WAIT_COMPLETED.name,
    ],
)
async def test_cancellation_type(
    env: WorkflowEnvironment,
    cancellation_type: Optional[str],
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    client = env.client

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[CallerWorkflow, HandlerWorkflow],
        nexus_service_handlers=[ServiceHandler()],
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, client)

        input = Input(
            endpoint=make_nexus_endpoint_name(worker.task_queue),
            cancellation_type=(
                workflow.NexusOperationCancellationType[cancellation_type]
                if cancellation_type
                else None
            ),
        )
        caller_wf = await client.start_workflow(
            CallerWorkflow.run,
            input,
            id="caller-wf-" + str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        operation_token = (await caller_wf.result()).operation_token
        handler_wf = (
            nexus.WorkflowHandle[None]
            .from_token(operation_token)
            ._to_client_workflow_handle(client)
        )

        if input.cancellation_type == workflow.NexusOperationCancellationType.ABANDON:
            await check_behavior_for_abandon(caller_wf, handler_wf)
        elif (
            input.cancellation_type
            == workflow.NexusOperationCancellationType.TRY_CANCEL
        ):
            await check_behavior_for_try_cancel(caller_wf, handler_wf)
        elif input.cancellation_type in [
            None,
            workflow.NexusOperationCancellationType.WAIT_COMPLETED,
        ]:
            await check_behavior_for_wait_cancellation_completed(caller_wf, handler_wf)
        else:
            pytest.fail(f"Invalid cancellation type: {input.cancellation_type}")


async def _has_event(wf_handle: WorkflowHandle, event_type: EventType.ValueType):
    async for e in wf_handle.fetch_history_events():
        if e.event_type == event_type:
            return True
    return False


async def _get_event_time(
    wf_handle: WorkflowHandle,
    event_type: EventType.ValueType,
) -> datetime:
    async for event in wf_handle.fetch_history_events():
        if event.event_type == event_type:
            return event.event_time.ToDatetime().replace(tzinfo=timezone.utc)
    assert False, f"Event {event_type} not found in {wf_handle.id}"


async def _assert_event_subsequence(
    expected_events: list[tuple[WorkflowHandle, EventType.ValueType]],
) -> None:
    """
    Given a sequence of (WorkflowHandle, EventType) pairs, assert that the sorted sequence of events
    from both workflows contains that subsequence.
    """

    def _event_time(
        item: tuple[WorkflowHandle, HistoryEvent],
    ) -> datetime:
        return item[1].event_time.ToDatetime()

    all_events = []
    handles = {h for h, _ in expected_events}
    for h in handles:
        async for e in h.fetch_history_events():
            all_events.append((h, e))
    _all_events = iter(sorted(all_events, key=_event_time))
    _expected_events = iter(expected_events)

    previous_expected_handle, previous_expected_event_type_name = None, None
    for expected_handle, expected_event_type in _expected_events:
        expected_event_type_name = EventType.Name(expected_event_type).removeprefix(
            "EVENT_TYPE_"
        )
        has_expected = next(
            (
                (h, e)
                for h, e in _all_events
                if h == expected_handle and e.event_type == expected_event_type
            ),
            None,
        )
        if not has_expected:
            if previous_expected_handle is not None:
                prefix = f"After {previous_expected_event_type_name} in {previous_expected_handle.id}, "
            else:
                prefix = ""
            pytest.fail(
                f"{prefix}expected {expected_event_type_name} in {expected_handle.id}"
            )
        previous_expected_event_type_name = expected_event_type_name
        previous_expected_handle = expected_handle
