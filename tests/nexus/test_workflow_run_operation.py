import uuid
from dataclasses import dataclass
from typing import Any, Type

import pytest
from nexusrpc import Operation, service
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
)
from nexusrpc.handler._decorators import operation_handler

from temporalio import workflow
from temporalio.nexus._operation_handlers import WorkflowRunOperationHandler
from temporalio.nexus._workflow import WorkflowRunOperationContext
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import (
    Failure,
    ServiceClient,
    create_nexus_endpoint,
    dataclass_as_dict,
)

HTTP_PORT = 7243


@dataclass
class Input:
    value: str


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


# TODO(nexus-prerelease): this test dates from a point at which we were encouraging
# subclassing WorkflowRunOperationHandler as part of the public API. Leaving it in for
# now.
class MyOperation(WorkflowRunOperationHandler):
    def __init__(self):
        pass

    async def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        tctx = WorkflowRunOperationContext(ctx)
        handle = await tctx.start_workflow(
            EchoWorkflow.run,
            input.value,
            id=str(uuid.uuid4()),
        )
        return StartOperationResultAsync(handle.to_token())


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

    __expected__error__ = 500, "'dict' object has no attribute 'value'"


@service_handler(service=Service)
class SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition:
    # Despite the lack of annotations on the service impl, the service definition
    # provides the type needed to deserialize the input into Input so that input.value
    # succeeds.
    @operation_handler
    def op(self) -> OperationHandler:
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
        service=service_handler_cls.__nexus_service__.name,
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
        if hasattr(service_handler_cls, "__expected__error__"):
            status_code, message = service_handler_cls.__expected__error__
            assert resp.status_code == status_code
            failure = Failure(**resp.json())
            assert failure.message == message
        else:
            assert resp.status_code == 201


def server_address(env: WorkflowEnvironment) -> str:
    http_port = getattr(env, "_http_port", 7243)
    return f"http://127.0.0.1:{http_port}"
