import uuid

import httpx
import nexusrpc
import nexusrpc.handler

from temporalio.client import Client
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint

HTTP_PORT = 7243


def make_incrementer_service(op_names: list[str]) -> tuple[type, type]:
    #
    # service contract
    #

    op_contracts = {name: nexusrpc.contract.Operation[int, int] for name in op_names}
    contract_cls = nexusrpc.contract.service(type("ServiceContract", (), op_contracts))

    #
    # service impl
    #
    async def _increment_op(
        self,
        ctx: nexusrpc.handler.StartOperationContext,
        input: int,
    ) -> int:
        return input + 1

    op_factories = {
        name: nexusrpc.handler.sync_operation(_increment_op) for name in op_names
    }

    impl_cls = nexusrpc.handler.service_handler(service=contract_cls)(
        type("ServiceImpl", (), op_factories)
    )

    return contract_cls, impl_cls


async def test_handler_creation_at_run_time(client: Client):
    task_queue = str(uuid.uuid4())

    contract_cls, impl_cls = make_incrementer_service(["increment"])

    service = contract_cls.__name__
    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[impl_cls()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service}/increment",
                json=1,
                headers={},
            )
            print(f"\n\n{response.json()}\n\n")

            assert response.status_code == 200
            assert response.json() == 2
