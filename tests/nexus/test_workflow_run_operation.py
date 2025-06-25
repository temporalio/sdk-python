import uuid
from dataclasses import dataclass
from typing import Any, Type

import pytest
from nexusrpc import Operation, service
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    operation_handler,
    service_handler,
)

from temporalio import workflow
from temporalio.nexus.handler import WorkflowRunOperationHandler, start_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import ServiceClient, create_nexus_endpoint, dataclass_as_dict

HTTP_PORT = 7243


@dataclass
class Input:
    value: str


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


class MyOperation(WorkflowRunOperationHandler):
    async def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        token = await start_workflow(
            EchoWorkflow.run,
            input.value,
            id=str(uuid.uuid4()),
        )
        return StartOperationResultAsync(token.encode())


@service_handler
class SubclassingHappyPath:
    @operation_handler
    def op(self) -> OperationHandler[Input, str]:
        return MyOperation()


@service
class Service:
    op: Operation[Input, str]


@service_handler
class SubclassingNoInputOutputTypeAnnotationsWithoutServiceDefinition:
    @operation_handler
    def op(self) -> OperationHandler:
        return MyOperation()


@service_handler(service=Service)
class SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition:
    @operation_handler
    def op(self) -> OperationHandler[Input, str]:
        return MyOperation()


@pytest.mark.parametrize(
    "service_handler_cls",
    [
        SubclassingHappyPath,
        SubclassingNoInputOutputTypeAnnotationsWithoutServiceDefinition,
        SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition,
    ],
)
async def test_workflow_run_operation(
    env: WorkflowEnvironment,
    service_handler_cls: Type[Any],
):
    task_queue = str(uuid.uuid4())
    endpoint = (await create_nexus_endpoint(task_queue, env.client)).endpoint.id
    service_client = ServiceClient(
        server_address=server_address(env),
        endpoint=endpoint,
        service=service_handler_cls.__name__,
    )
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler_cls()],
    ):
        resp = await service_client.start_operation(
            "op",
            dataclass_as_dict(Input(value="test")),
        )
        assert resp.status_code == 201


def server_address(env: WorkflowEnvironment) -> str:
    http_port = getattr(env, "_http_port", 7243)
    return f"http://127.0.0.1:{http_port}"
