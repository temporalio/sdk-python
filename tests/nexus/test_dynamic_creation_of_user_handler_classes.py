import uuid

import httpx
import nexusrpc.handler
import pytest
from nexusrpc.handler import sync_operation

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.nexus._util import get_operation_factory
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint

HTTP_PORT = 7243


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: int) -> int:
        return input + 1


@nexusrpc.handler.service_handler
class MyServiceHandlerWithWorkflowRunOperation:
    @nexus.workflow_run_operation
    async def increment(
        self,
        ctx: nexus.WorkflowRunOperationContext,
        input: int,
    ) -> nexus.WorkflowHandle[int]:
        return await ctx.start_workflow(MyWorkflow.run, input, id=str(uuid.uuid4()))


async def test_run_nexus_service_from_programmatically_created_service_handler(
    client: Client,
):
    task_queue = str(uuid.uuid4())

    user_service_handler_instance = MyServiceHandlerWithWorkflowRunOperation()
    service_handler = nexusrpc.handler._core.ServiceHandler.from_user_instance(
        user_service_handler_instance
    )

    assert (
        service_defn := nexusrpc.get_service_definition(
            user_service_handler_instance.__class__
        )
    )
    service_name = service_defn.name

    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service_name}/increment",
                json=1,
            )
            assert response.status_code == 201


def make_incrementer_user_service_definition_and_service_handler_classes(
    op_names: list[str],
) -> tuple[type, type]:
    #
    # service contract
    #

    ops = {name: nexusrpc.Operation[int, int] for name in op_names}
    service_cls: type = nexusrpc.service(type("ServiceContract", (), ops))

    #
    # service handler
    #
    @sync_operation
    async def _increment_op(
        self,
        ctx: nexusrpc.handler.StartOperationContext,
        input: int,
    ) -> int:
        return input + 1

    op_handler_factories = {}
    for name in op_names:
        op_handler_factory, _ = get_operation_factory(_increment_op)
        assert op_handler_factory
        op_handler_factories[name] = op_handler_factory

    handler_cls: type = nexusrpc.handler.service_handler(service=service_cls)(
        type("ServiceImpl", (), op_handler_factories)
    )

    return service_cls, handler_cls


@pytest.mark.skip(
    reason="Dynamic creation of service contract using type() is not supported"
)
async def test_dynamic_creation_of_user_handler_classes(client: Client):
    task_queue = str(uuid.uuid4())

    service_cls, handler_cls = (
        make_incrementer_user_service_definition_and_service_handler_classes(
            ["increment"]
        )
    )

    assert (service_defn := nexusrpc.get_service_definition(service_cls))
    service_name = service_defn.name

    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[handler_cls()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service_name}/increment",
                json=1,
            )
            assert response.status_code == 200
            assert response.json() == 2
