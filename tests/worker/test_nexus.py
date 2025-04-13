import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Union

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
from temporalio.client import Client
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


ResponseType = Union[SyncResponse, AsyncResponse]


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

    @workflow.run
    async def run(self, response_type: ResponseType, should_cancel: bool) -> str:
        handle = await self.nexus_service.start_operation(
            MyService.my_operation, MyInput(response_type)
        )
        if should_cancel:
            assert handle.cancel()
            return ""
        else:
            result = await handle
            return result.val


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
        result = await client.execute_workflow(
            MyCallerWorkflow.run,
            args=[SyncResponse(), False],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == "sync response"


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
