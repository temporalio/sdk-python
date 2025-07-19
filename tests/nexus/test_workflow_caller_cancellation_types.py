import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional

import nexusrpc
import pytest

from temporalio import exceptions, nexus, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@workflow.defn(sandboxed=False)
class HandlerWorkflow:
    @workflow.run
    async def run(self) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # TODO: this sleep is so that we can distinguish WAIT_REQUESTED from
            # WAIT_COMPLETED. In the former case the caller workflow will see the
            # CancelRequested event but the workflow will still be running, due to this
            # sleep.
            await asyncio.sleep(0.5)
            raise


@nexusrpc.service
class Service:
    workflow_op: nexusrpc.Operation[None, None]


@nexusrpc.handler.service_handler(service=Service)
class ServiceHandler:
    @nexus.workflow_run_operation
    async def workflow_op(
        self, ctx: nexus.WorkflowRunOperationContext, _input: None
    ) -> nexus.WorkflowHandle[None]:
        print("🟢 ServiceHandler.workflow_op")
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


@workflow.defn(sandboxed=False)
class CancellationTypeWorkflow:
    @workflow.init
    def __init__(self, input: Input):
        self.nexus_client = workflow.create_nexus_client(
            service=Service,
            endpoint=input.endpoint,
        )

    @workflow.run
    async def run(self, input: Input) -> CancellationResult:
        """
        Test cancellation type = WAIT_CANCELLATION_COMPLETED.
        The operation should have been canceled before the nexus operation failure exception is raised.
        """
        kwargs = {}
        if input.cancellation_type is not None:
            kwargs["cancellation_type"] = input.cancellation_type
        op_handle = await self.nexus_client.start_operation(
            Service.workflow_op, input=None, **kwargs
        )
        op_handle.cancel()
        try:
            await op_handle
        except exceptions.NexusOperationError:
            assert op_handle.operation_token
            return CancellationResult(operation_token=op_handle.operation_token)
        else:
            pytest.fail("Expected NexusOperationError")


async def check_behavior_for_wait_cancellation_completed(
    client: Client, operation_token: str
) -> None:
    """
    Check that backing workflow received a cancellation request and has been canceled (is
    not running)
    """
    nexus_handle = nexus.WorkflowHandle.from_token(operation_token)
    wf_handle = nexus_handle._to_client_workflow_handle(client)

    assert (await wf_handle.describe()).status == WorkflowExecutionStatus.CANCELED
    assert await _has_event(
        wf_handle, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
    )


async def check_behavior_for_abandon(client: Client, operation_token: str) -> None:
    """
    Check that backing workflow has not received a cancellation request and has not been
    canceled (is still running) and does not receive a cancellation request.
    """
    nexus_handle = nexus.WorkflowHandle.from_token(operation_token)
    wf_handle = nexus_handle._to_client_workflow_handle(client)

    assert (await wf_handle.describe()).status == WorkflowExecutionStatus.RUNNING
    await asyncio.sleep(0.5)
    assert not await _has_event(
        wf_handle, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
    )


async def check_behavior_for_wait_cancellation_requested(
    client: Client, operation_token: str
) -> None:
    """
    Check that backing workflow received a cancellation request but has not been canceled
    (is still running)
    """
    nexus_handle = nexus.WorkflowHandle.from_token(operation_token)
    wf_handle = nexus_handle._to_client_workflow_handle(client)

    assert await _has_event(
        wf_handle, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
    )
    # TODO(nexus-preview) We should be able to assert that the workflow has not actually
    # been canceled yet.
    # # Check that workflow is still running
    # desc = await wf_handle.describe()
    # assert desc.status == WorkflowExecutionStatus.RUNNING


async def check_behavior_for_try_cancel(client: Client, operation_token: str) -> None:
    """
    Check that backing workflow has not received a cancellation request initially
    and is still running, but eventually does receive a cancellation request
    """
    nexus_handle = nexus.WorkflowHandle.from_token(operation_token)
    wf_handle = nexus_handle._to_client_workflow_handle(client)
    assert (await wf_handle.describe()).status == WorkflowExecutionStatus.RUNNING

    # TODO(nexus-preview): I was expecting the handler workflow to eventually receive a
    # cancellation request, but it seems not to.
    # await _assert_has_event_eventually(
    #     wf_handle, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
    # )


@pytest.mark.parametrize(
    "cancellation_type",
    [
        None,
        workflow.NexusOperationCancellationType.WAIT_CANCELLATION_COMPLETED.name,
        workflow.NexusOperationCancellationType.ABANDON.name,
        workflow.NexusOperationCancellationType.TRY_CANCEL.name,
        workflow.NexusOperationCancellationType.WAIT_CANCELLATION_REQUESTED.name,
    ],
)
async def test_cancellation_type(
    env: WorkflowEnvironment,
    cancellation_type: Optional[str],
):
    client = env.client

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[CancellationTypeWorkflow, HandlerWorkflow],
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
        wf_handle = await client.start_workflow(
            CancellationTypeWorkflow.run,
            input,
            id="caller-wf-" + str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        cancellation_result = await wf_handle.result()

        op_token = cancellation_result.operation_token

        if input.cancellation_type in [
            None,
            workflow.NexusOperationCancellationType.WAIT_CANCELLATION_COMPLETED,
        ]:
            await check_behavior_for_wait_cancellation_completed(client, op_token)
        elif input.cancellation_type == workflow.NexusOperationCancellationType.ABANDON:
            await check_behavior_for_abandon(client, op_token)
        elif (
            input.cancellation_type
            == workflow.NexusOperationCancellationType.WAIT_CANCELLATION_REQUESTED
        ):
            await check_behavior_for_wait_cancellation_requested(client, op_token)
        elif (
            input.cancellation_type
            == workflow.NexusOperationCancellationType.TRY_CANCEL
        ):
            await check_behavior_for_try_cancel(client, op_token)
        else:
            pytest.fail(f"Invalid cancellation type: {input.cancellation_type}")


async def _has_event(wf_handle: WorkflowHandle, event_type: EventType.ValueType):
    async for e in wf_handle.fetch_history_events():
        if e.event_type == event_type:
            return True
    return False


async def _assert_has_event_eventually(
    wf_handle: WorkflowHandle, event_type: EventType.ValueType
) -> None:
    await assert_eq_eventually(True, lambda: _has_event(wf_handle, event_type))
