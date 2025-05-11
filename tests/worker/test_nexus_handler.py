import asyncio
import uuid
from dataclasses import dataclass
from typing import Never, Tuple

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
    hang: nexusrpc.interface.Operation[Input, Output]


@nexusrpc.handler.service(interface=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation
    async def echo(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        assert options.headers["test-header-key"] == "test-header-value"
        return Output(value=f"from handler: {input.value}")

    @nexusrpc.handler.sync_operation
    async def hang(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Never:
        await asyncio.Future()


async def test_success(http_test_env: Tuple[Client, int]):
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
                    "Test-Header-Key": "test-header-value",
                    "Nexus-Link": '<http://test/>; type="test"',
                },
            )
            assert response.is_success
            response.raise_for_status()
            # Check if the Nexus-Link header is echoed in the response
            # TODO(dan): Support manually adding links in operation handler
            # See e.g. TS nexus.handlerLinks().push(...options.links)
            # assert response.headers.get("nexus-link") == "<http://test/>; type=\"test\"", \
            #     "Nexus-Link header not echoed correctly."
            output_json = response.json()
            assert output_json == {"value": "from handler: hello"}


# TODO(dan): Why are we seeing 2025-05-11T22:41:51.853243Z  WARN temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value '5.617792ms'
async def test_upstream_timeout(http_test_env: Tuple[Client, int]):
    client, http_port = http_test_env

    task_queue = str(uuid.uuid4())
    service = MyService.__name__
    operation = "hang"
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
                    "Request-Timeout": "10ms",
                },
            )
            assert response.status_code == 520
