import uuid

import httpx
import nexusrpc
import nexusrpc.handler

from temporalio.client import Client
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint

HTTP_PORT = 7243


def make_incrementer_service_from_service_handler(
    op_names: list[str],
) -> tuple[str, type]:
    pass


def make_incrementer_service_from_user_classes(
    op_names: list[str],
) -> tuple[str, type]:
    #
    # service contract
    #

    ops = {name: nexusrpc.contract.Operation[int, int] for name in op_names}
    service_cls = nexusrpc.contract.service(type("ServiceContract", (), ops))

    #
    # service handler
    #
    async def _increment_op(
        self,
        ctx: nexusrpc.handler.StartOperationContext,
        input: int,
    ) -> int:
        return input + 1

    op_handler_factories = {
        # TODO(dan): check that name=name should be required here. Should the op factory
        # name not default to the name of the method attribute (i.e. key), as opposed to
        # the name of the method object (i.e. value.__name__)?
        name: nexusrpc.handler.sync_operation_handler(_increment_op, name=name)
        for name in op_names
    }

    handler_cls = nexusrpc.handler.service_handler(service=service_cls)(
        type("ServiceImpl", (), op_handler_factories)
    )

    return service_cls.__name__, handler_cls


async def test_dynamic_creation_of_user_handler_classes(client: Client):
    task_queue = str(uuid.uuid4())

    service_name, handler_cls = make_incrementer_service_from_user_classes(
        ["increment"]
    )

    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[handler_cls()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service_name}/increment",
                json=1,
                headers={},
            )
            print(f"\n\n{response.json()}\n\n")

            assert response.status_code == 200
            assert response.json() == 2
