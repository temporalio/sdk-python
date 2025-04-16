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


# The ordering in this union is critical since the data converter matches eagerly,
# ignoring unknown fields, and so would identify an AsyncResponse as a SyncResponse if
# SyncResponse came first in the union.

# TODO(dan): Using Unions with the data converter is probably inadvisable.
ResponseType = Union[AsyncResponse, SyncResponse]


@dataclass
class MyInput:
    response_type: ResponseType
    block_forever_waiting_for_cancellation: bool


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
                input.block_forever_waiting_for_cancellation,
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
        should_cancel: bool,
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
        should_cancel: bool,
        task_queue: str,
    ) -> str:
        op_handle = await self.nexus_service.start_operation(
            MyService.my_operation,
            MyInput(
                response_type,
                block_forever_waiting_for_cancellation=should_cancel,
            ),
        )
        self._nexus_operation_started = True
        if isinstance(response_type, SyncResponse):
            assert op_handle.operation_token is None
        else:
            assert op_handle.operation_token
            # Allow the test to make assertions before signalling us to proceed.
            await workflow.wait_condition(lambda: self._proceed)

        if should_cancel:
            op_handle.cancel()

        result = await op_handle
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


# TODO(dan): cross-namespace tests
# TODO(dan): nexus endpoint pytest fixture?
@pytest.mark.parametrize("should_attempt_cancel", [False, True])
async def test_sync_response(client: Client, should_attempt_cancel: bool):
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
            args=[SyncResponse(), should_attempt_cancel, task_queue],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        # The response is synchronous, so the workflow's attempt to cancel the
        # NexusOperationHandle does not result in cancellation.
        result = await wf_handle.result()
        assert result == "sync response"


async def test_async_response(client: Client):
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
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[AsyncResponse(operation_workflow_id), False, task_queue],
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

        # Wait for the Nexus operation to complete and check that the operation-backing
        # workflow has completed.
        await wf_handle.signal(MyCallerWorkflow.proceed)

        wf_details = await operation_workflow_handle.describe()
        assert wf_details.status == WorkflowExecutionStatus.COMPLETED
        result = await wf_handle.result()
        assert result == "workflow result"


async def test_cancellation_of_async_response(client: Client):
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
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[AsyncResponse(operation_workflow_id), True, task_queue],
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
        # The caller workflow will now cancel the op_handle, and await it.

        # TODO(dan): assert what type of exception is raised here
        with pytest.raises(BaseException) as ei:
            await wf_handle.result()
        e = ei.value
        print(f"🌈 workflow failed: {e.__class__.__name__}({e})")
        wf_details = await operation_workflow_handle.describe()
        assert wf_details.status == WorkflowExecutionStatus.CANCELED


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
