import uuid
from dataclasses import dataclass

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
class MyService:
    @operation_handler
    def op(self) -> OperationHandler[Input, str]:
        return MyOperation()


async def test_workflow_run_operation_via_subclassing(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    endpoint = (await create_nexus_endpoint(task_queue, env.client)).endpoint.id
    service_client = ServiceClient(
        server_address=server_address(env),
        endpoint=endpoint,
        service=MyService.__name__,
    )
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[MyService()],
    ):
        resp = await service_client.start_operation(
            "op",
            dataclass_as_dict(Input(value="test")),
        )
        assert resp.status_code == 201


def server_address(env: WorkflowEnvironment) -> str:
    http_port = getattr(env, "_http_port", 7243)
    return f"http://127.0.0.1:{http_port}"
