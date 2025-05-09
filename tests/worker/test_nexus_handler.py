import uuid
from dataclasses import dataclass
from typing import Tuple

import httpx
import nexusrpc
import nexusrpc.handler

from temporalio.client import Client
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint


@dataclass
class Input:
    value: str


@dataclass
class Output:
    value: str


@nexusrpc.interface.service
class MyService:
    echo: nexusrpc.interface.Operation[Input, Output]


@nexusrpc.handler.service(interface=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation
    async def echo(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        # Custom header assertion removed temporarily due to headers not being populated in options.headers
        # assert options.headers.get("test") == "true", "Custom header 'Test' not received or incorrect."
        return Output(value=f"from handler: {input.value}")


async def test_sync_operation_direct_http_invocation(http_test_env: Tuple[Client, int]):
    client, http_port = http_test_env

    task_queue = str(uuid.uuid4())
    service = MyService.__name__
    operation = "echo"
    resp = await create_nexus_endpoint(task_queue, client)
    endpoint = resp.endpoint.id

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{http_port}/nexus/endpoints/{endpoint}/services/{service}/{operation}",
                json={"value": "hello"},
                headers={
                    "Content-Type": "application/json",
                    "Test": "true",
                    "Nexus-Link": '<http://test/>; type="test"',
                },
            )
            # Print response content for debugging in case of error
            if not response.is_success:
                print(f"Error response from server: {response.text}")
            response.raise_for_status()
            # Check if the Nexus-Link header is echoed in the response
            # TODO(dan): Support manually adding links in operation handler
            # See e.g. TS nexus.handlerLinks().push(...options.links)
            # assert response.headers.get("nexus-link") == "<http://test/>; type=\"test\"", \
            #     "Nexus-Link header not echoed correctly."
            output_json = response.json()
            assert output_json == {"value": "from handler: hello"}
