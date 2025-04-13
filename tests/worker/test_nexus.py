import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Union, cast

import nexus
import nexus.handler

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

NEXUS_ENDPOINT_NAME = "my-nexus-endpoint"


# -----------------------------------------------------------------------------
# Service interface
#
@dataclass
class SyncResponse:
    pass


@dataclass
class AsyncResponse:
    operation_workflow_id: str


# TODO(dan): Using Unions with the data converter is probably inadvisable.
# They must be in this since the data converter matches eagerly, ignoring unknown fields.
ResponseType = Union[AsyncResponse, SyncResponse]


@dataclass
class MyInput:
    response_type: ResponseType


@dataclass
class MyOutput:
    val: str


@nexus.service
class MyService:
    my_operation: nexus.Operation[MyInput, MyOutput]


# -----------------------------------------------------------------------------
# Service implementation
#


@workflow.defn
class MyHandlerWorkflow:
    @workflow.run
    async def run(self) -> MyOutput:
        return MyOutput("workflow result")


class MyOperation:
    async def start(
        self, input: MyInput, options: nexus.handler.StartOperationOptions
    ) -> Union[
        MyOutput,
        temporalio.nexus.handler.AsyncWorkflowOperationResult[MyOutput],
    ]:
        if isinstance(input.response_type, SyncResponse):
            return MyOutput(val="sync response")
        elif isinstance(input.response_type, AsyncResponse):
            return await temporalio.nexus.handler.start_workflow(
                MyHandlerWorkflow.run,
                id=input.response_type.operation_workflow_id,
                options=options,
            )
        else:
            raise TypeError

    async def cancel(
        self, token: str, options: nexus.handler.CancelOperationOptions
    ) -> None:
        return await temporalio.nexus.handler.cancel_workflow(token, options)

    async def fetch_info(self, *args, **kwargs):
        raise NotImplementedError

    async def fetch_result(self, *args, **kwargs):
        raise NotImplementedError


@nexus.handler.service(interface=MyService)
class MyServiceImpl:
    @nexus.handler.operation
    def my_operation(self) -> nexus.handler.Operation[MyInput, MyOutput]:
        return MyOperation()


# -----------------------------------------------------------------------------
# Caller workflow
#
@workflow.defn
class MyCallerWorkflow:
    def __init__(self) -> None:
        self.nexus_service = workflow.NexusClient(
            service=MyService,
            endpoint=NEXUS_ENDPOINT_NAME,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        self._waiting_to_proceed = False
        self._proceed = False

    @workflow.run
    async def run(self, response_type: ResponseType, should_cancel: bool) -> str:
        op_handle = await self.nexus_service.start_operation(
            MyService.my_operation, MyInput(response_type)
        )
        task = cast(asyncio.Task, getattr(op_handle, "_task"))
        if isinstance(response_type, SyncResponse):
            assert op_handle.operation_token is None
            # TODO(dan): I expected task to be done at this point
            # assert task.done()
            # assert not task.exception()
        else:
            assert op_handle.operation_token
            assert not task.done()
            self._waiting_to_proceed = True
            await workflow.wait_condition(lambda: self._proceed)

        if should_cancel:
            assert op_handle.cancel()
            return ""
        else:
            result = await op_handle
            return result.val

    @workflow.update
    async def wait_nexus_operation_started(self) -> None:
        await workflow.wait_condition(lambda: self._waiting_to_proceed)

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


# TODO(dan): cross-namespace tests
# TODO(dan): nexus endpoint pytest fixture?
async def test_sync_response(client: Client):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_services=[MyServiceImpl()],
        workflows=[MyCallerWorkflow, MyHandlerWorkflow],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await create_nexus_endpoint(client, task_queue)
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[SyncResponse(), False],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

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
        await create_nexus_endpoint(client, task_queue)

        # Start the caller workflow
        wf_handle = await client.start_workflow(
            MyCallerWorkflow.run,
            args=[AsyncResponse(operation_workflow_id), False],
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


async def create_nexus_endpoint(client: Client, task_queue: str) -> None:
    try:
        await client.operator_service.create_nexus_endpoint(
            temporalio.api.operatorservice.v1.CreateNexusEndpointRequest(
                spec=temporalio.api.nexus.v1.EndpointSpec(
                    name=NEXUS_ENDPOINT_NAME,
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
            print(f"Nexus endpoint {NEXUS_ENDPOINT_NAME} already exists")
        else:
            raise
