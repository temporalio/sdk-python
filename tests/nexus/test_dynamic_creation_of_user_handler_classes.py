import uuid

import httpx
import nexusrpc.handler
import pytest
from nexusrpc.handler import sync_operation

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.nexus._util import get_operation_factory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import ServiceClient, make_nexus_endpoint_name


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

    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
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
        _self,  # type:ignore[reportMissingParameterType]
        _ctx: nexusrpc.handler.StartOperationContext,
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
async def test_dynamic_creation_of_user_handler_classes(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())

    service_cls, handler_cls = (
        make_incrementer_user_service_definition_and_service_handler_classes(
            ["increment"]
        )
    )

    assert (service_defn := nexusrpc.get_service_definition(service_cls))
    service_name = service_defn.name

    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[handler_cls()],
    ):
        server_address = ServiceClient.default_server_address(env)
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://{server_address}/nexus/endpoints/{endpoint}/services/{service_name}/increment",
                json=1,
            )
            assert response.status_code == 200
            assert response.json() == 2
