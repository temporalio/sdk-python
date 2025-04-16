import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Union

import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.api
import temporalio.api.common
import temporalio.api.common.v1
import temporalio.api.nexus
import temporalio.api.nexus.v1
import temporalio.api.operatorservice
import temporalio.api.operatorservice.v1
import temporalio.nexus
import temporalio.nexus.handler
from temporalio import workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


# -----------------------------------------------------------------------------
# Service interface
#
@dataclass
class SyncResponse:
    pass


@dataclass
class AsyncResponse:
    operation_workflow_id: str
    block_forever_waiting_for_cancellation: bool


# The ordering in this union is critical since the data converter matches eagerly,
# ignoring unknown fields, and so would identify an AsyncResponse as a SyncResponse if
# SyncResponse came first in the union.

# TODO(dan): Using Unions with the data converter is probably inadvisable.
ResponseType = Union[AsyncResponse, SyncResponse]


@dataclass
class MyInput:
    response_type: ResponseType


@dataclass
class MyOutput:
    val: str


@nexusrpc.service
class MyService:
    my_operation: nexusrpc.Operation[MyInput, MyOutput]


# -----------------------------------------------------------------------------
# Service implementation
#


@workflow.defn
class MyHandlerWorkflow:
    @workflow.run
    async def run(self, block_forever_waiting_for_cancellation: bool) -> MyOutput:
        if block_forever_waiting_for_cancellation:
            await workflow.wait_condition(lambda: False)
        return MyOutput("workflow result")


class MyOperation:
    async def start(
        self, input: MyInput, options: nexusrpc.handler.StartOperationOptions
    ) -> Union[
        MyOutput,
        temporalio.nexus.handler.AsyncWorkflowOperationResult[MyOutput],
    ]:
        if isinstance(input.response_type, SyncResponse):
            return MyOutput(val="sync response")
        elif isinstance(input.response_type, AsyncResponse):
            return await temporalio.nexus.handler.start_workflow(
                MyHandlerWorkflow.run,
                input.response_type.block_forever_waiting_for_cancellation,
                id=input.response_type.operation_workflow_id,
                options=options,
            )
        else:
            raise TypeError

    async def cancel(
        self, token: str, options: nexusrpc.handler.CancelOperationOptions
    ) -> None:
        return await temporalio.nexus.handler.cancel_workflow(token, options)

    async def fetch_info(self, *args, **kwargs):
        raise NotImplementedError

    async def fetch_result(self, *args, **kwargs):
        raise NotImplementedError


@nexusrpc.handler.service(interface=MyService)
class MyServiceImpl:
    @nexusrpc.handler.operation
    def my_operation(self) -> nexusrpc.handler.Operation[MyInput, MyOutput]:
        return MyOperation()


# -----------------------------------------------------------------------------
# Caller workflow
#
@workflow.defn
class MyCallerWorkflow:
    """
    A workflow that executes a Nexus operation, specifying whether it should return
    synchronously or asynchronously.
    """

    @workflow.init
    def __init__(
        self,
        response_type: ResponseType,
        request_cancel: bool,
        task_queue: str,
    ) -> None:
        self.nexus_service = workflow.NexusClient(
            service=MyService,
            endpoint=make_nexus_endpoint_name(task_queue),
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        self._nexus_operation_started = False
        self._proceed = False

    @workflow.run
    async def run(
        self,
        response_type: ResponseType,
        request_cancel: bool,
        task_queue: str,
    ) -> str:
        op_handle = await self.nexus_service.start_operation(
            MyService.my_operation,
            MyInput(response_type),
        )
        print(f"🌈 {'after await start':<24}: {op_handle}")
        self._nexus_operation_started = True
        if isinstance(response_type, SyncResponse):
            assert op_handle.operation_token is None
        else:
            assert op_handle.operation_token
            # Allow the test to make assertions before signalling us to proceed.
            await workflow.wait_condition(lambda: self._proceed)
            print(f"🌈 {'after await proceed':<24}: {op_handle}")

        if request_cancel:
            # Even for SyncResponse, the op_handle future is not done at this point; that
            # transition doesn't happen until the handle is awaited.
            print(f"🌈 {'before op_handle.cancel':<24}: {op_handle}")
            cancel_ret = op_handle.cancel()
            print(f"🌈 {'cancel returned':<24}: {cancel_ret}")

        print(f"🌈 {'before await op_handle':<24}: {op_handle}")
        result = await op_handle
        print(f"🌈 {'after await op_handle':<24}: {op_handle}")
        return result.val

    @workflow.update
    async def wait_nexus_operation_started(self) -> None:
        await workflow.wait_condition(lambda: self._nexus_operation_started)

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


# -----------------------------------------------------------------------------
# Tests
#


# When request_cancel is True, the NexusOperationHandle in the workflow evolves
# through the following states:
#                           start_fut             result_fut            handle_task w/ fut_waiter                        (task._must_cancel)
#
# Case 1: Sync Nexus operation response w/ cancellation of NexusOperationHandle
# -----------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# before op_handle.cancel : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# Future_8240[FINISHED].cancel() -> False  # no state transition; fut_waiter is already finished
# cancel returned         : True
# before await op_handle  : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (True)
# --> Despite cancel having been requested, this await on the nexus op handle does not
#     raise CancelledError, because the task's underlying fut_waiter is already finished.
# after await op_handle   : Future_7856[FINISHED] Future_7984[FINISHED] Task[FINISHED] fut_waiter = None) (False)
# <<<<<<<<<<<< END WFT 1
#

# Case 2: Async Nexus operation response w/ cancellation of NexusOperationHandle
# ------------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# >>>>>>>>>>>> WFT 2
# >>>>>>>>>>>> WFT 3
# after await proceed     : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# before op_handle.cancel : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# Future_7952[PENDING].cancel() -> True  # transition to cancelled state; fut_waiter was not finished
# cancel returned         : True
# before await op_handle  : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[CANCELLED]) (False)
# --> This await on the nexus op handle raises CancelledError, because the task's underlying fut_waiter is cancelled.

# Thus in the sync case, although the caller workflow attempted to cancel the
# NexusOperationHandle, this did not result in a CancelledError when the handle was
# awaited, because both resolve_nexus_operation_start and resolve_nexus_operation jobs
# were sent in the same activation and hence the task's fut_waiter was already finished.
#
# But in the async case, at the time that we cancel the NexusOperationHandle, only the
# resolve_nexus_operation_start job had been sent; the result_fut was unresolved. Thus
# when the handle was awaited, CancelledError was raised.

# To create output like that above, set the following __repr__s:
# asyncio.Future:
# def __repr__(self):
#     return f"{self.__class__.__name__}_{str(id(self))[-4:]}[{self._state}]"
# _NexusOperationHandle:
# def __repr__(self) -> str:
#     return (
#         f"{self._start_fut} "
#         f"{self._result_fut} "
#         f"Task[{self._task._state}] fut_waiter = {self._task._fut_waiter}) ({self._task._must_cancel})"
#     )


# TODO(dan): cross-namespace tests
# TODO(dan): nexus endpoint pytest fixture?
# TODO: get rid of UnsandboxedWorkflowRunner (due to xray)
@pytest.mark.parametrize("request_cancel", [False, True])
async def test_sync_response(client: Client, request_cancel: bool):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_services=[MyServiceImpl()],
        workflows=[MyCallerWorkflow, MyHandlerWorkflow],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await create_nexus_endpoint(task_queue, client)
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[SyncResponse(), request_cancel, task_queue],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # The operation result is returned even when request_cancel=True, because the
        # response was synchronous and it could not be cancelled. See explanation above.
        result = await wf_handle.result()
        assert result == "sync response"


@pytest.mark.parametrize("request_cancel", [False, True])
async def test_async_response(client: Client, request_cancel: bool):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_services=[MyServiceImpl()],
        workflows=[MyCallerWorkflow, MyHandlerWorkflow],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        operation_workflow_id = str(uuid.uuid4())
        operation_workflow_handle = client.get_workflow_handle(operation_workflow_id)
        await create_nexus_endpoint(task_queue, client)

        # Start the caller workflow
        block_forever_waiting_for_cancellation = request_cancel
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[
                AsyncResponse(
                    operation_workflow_id, block_forever_waiting_for_cancellation
                ),
                request_cancel,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # Wait for the Nexus operation to start and check that the operation-backing workflow now exists.
        await wf_handle.execute_update(MyCallerWorkflow.wait_nexus_operation_started)
        wf_details = await operation_workflow_handle.describe()
        assert wf_details.status in [
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
        ]

        await wf_handle.signal(MyCallerWorkflow.proceed)

        # The operation response was asynchronous and so request_cancel is honored. See
        # explanation above.
        if request_cancel:
            # The caller workflow now cancels the op_handle, and awaits it, resulting in a
            # CancellationError in the caller workflow.
            with pytest.raises(BaseException) as ei:
                await wf_handle.result()
            e = ei.value
            print(f"🌈 workflow failed: {e.__class__.__name__}({e})")
            wf_details = await operation_workflow_handle.describe()
            assert wf_details.status == WorkflowExecutionStatus.CANCELED
        else:
            wf_details = await operation_workflow_handle.describe()
            assert wf_details.status == WorkflowExecutionStatus.COMPLETED
            result = await wf_handle.result()
            assert result == "workflow result"


def make_nexus_endpoint_name(task_queue: str) -> str:
    return f"nexus-endpoint-{task_queue}"


async def create_nexus_endpoint(task_queue: str, client: Client) -> None:
    # In order to be able to create endpoints for different task queues without risk of
    # name collision.
    name = make_nexus_endpoint_name(task_queue)
    try:
        await client.operator_service.create_nexus_endpoint(
            temporalio.api.operatorservice.v1.CreateNexusEndpointRequest(
                spec=temporalio.api.nexus.v1.EndpointSpec(
                    name=name,
                    target=temporalio.api.nexus.v1.EndpointTarget(
                        worker=temporalio.api.nexus.v1.EndpointTarget.Worker(
                            namespace=client.namespace,
                            task_queue=task_queue,
                        )
                    ),
                )
            )
        )
    except RPCError as e:
        if e.status == RPCStatusCode.ALREADY_EXISTS:
            print(f"🟠 Nexus endpoint {name} already exists")
        else:
            raise


# TODO(dan): test exceptions in Nexus worker are raised
