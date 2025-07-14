import uuid

import httpx
import nexusrpc.handler
import pytest

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import ServiceClient, create_nexus_endpoint


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: int) -> int:
        return input + 1


@nexusrpc.service
class MyService:
    increment: nexusrpc.Operation[int, int]


class MyIncrementOperationHandler(nexusrpc.handler.OperationHandler[int, int]):
    async def start(
        self,
        ctx: nexusrpc.handler.StartOperationContext,
        input: int,
    ) -> nexusrpc.handler.StartOperationResultAsync:
        wrctx = nexus.WorkflowRunOperationContext._from_start_operation_context(ctx)
        wf_handle = await wrctx.start_workflow(
            MyWorkflow.run, input, id=str(uuid.uuid4())
        )
        return nexusrpc.handler.StartOperationResultAsync(token=wf_handle.to_token())

    async def cancel(
        self,
        ctx: nexusrpc.handler.CancelOperationContext,
        token: str,
    ) -> None:
        raise NotImplementedError

    async def fetch_info(
        self,
        ctx: nexusrpc.handler.FetchOperationInfoContext,
        token: str,
    ) -> nexusrpc.OperationInfo:
        raise NotImplementedError

    async def fetch_result(
        self,
        ctx: nexusrpc.handler.FetchOperationResultContext,
        token: str,
    ) -> int:
        raise NotImplementedError


@nexusrpc.handler.service_handler
class MyServiceHandlerWithWorkflowRunOperation:
    @nexusrpc.handler._decorators.operation_handler
    def increment(self) -> nexusrpc.handler.OperationHandler[int, int]:
        return MyIncrementOperationHandler()


async def test_run_nexus_service_from_programmatically_created_service_handler(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())

    service_handler = nexusrpc.handler._core.ServiceHandler(
        service=nexusrpc.ServiceDefinition(
            name="MyService",
            operation_definitions={
                "increment": nexusrpc.OperationDefinition[int, int](
                    name="increment",
                    method_name="increment",
                    input_type=int,
                    output_type=int,
                ),
            },
        ),
        operation_handlers={
            "increment": MyIncrementOperationHandler(),
        },
    )

    service_name = service_handler.service.name

    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
    ):
        server_address = ServiceClient.default_server_address(env)
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://{server_address}/nexus/endpoints/{endpoint}/services/{service_name}/increment",
                json=1,
            )
            assert response.status_code == 201
