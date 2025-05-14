import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Never, Tuple

import httpx
import nexusrpc
import nexusrpc.handler
import pytest

from temporalio.client import Client
from temporalio.nexus import logger
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
    log: nexusrpc.interface.Operation[Input, Output]


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

    @nexusrpc.handler.sync_operation
    async def log(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        logger.info("Logging from handler", extra={"input_value": input.value})
        return Output(value=f"logged: {input.value}")


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


@dataclass
class TestCase:
    test_name: str
    operation: str
    json: dict[str, Any]
    headers: dict[str, str]
    expected_status_code: int


@pytest.mark.parametrize(
    "test_case",
    [
        # TODO(dan): Why are we seeing 2025-05-11T22:41:51.853243Z  WARN temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value '5.617792ms'
        TestCase(
            test_name="upstream_timeout",
            operation="hang",
            json={"value": "hello"},
            headers={"Request-Timeout": "10ms"},
            expected_status_code=520,
        ),
        TestCase(
            test_name="test_bad_request",
            operation="echo",
            json={"value": 7},
            headers={"Content-Type": "application/json"},
            expected_status_code=400,
        ),
    ],
)
async def test_nexus_handler_failure(
    test_case: TestCase, http_test_env: Tuple[Client, int]
):
    client, http_port = http_test_env
    task_queue = str(uuid.uuid4())
    service = MyService.__name__
    resp = await create_nexus_endpoint(task_queue, client)
    endpoint = resp.endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{http_port}/nexus/endpoints/{endpoint}/services/{service}/{test_case.operation}",
                json=test_case.json,
                headers=test_case.headers,
            )
            assert response.status_code == test_case.expected_status_code


async def test_logging_in_operation_handler(
    http_test_env: Tuple[Client, int], caplog: Any
):
    client, http_port = http_test_env
    task_queue = str(uuid.uuid4())
    service_name = MyService.__name__
    operation_name = "log"
    resp = await create_nexus_endpoint(task_queue, client)
    endpoint = resp.endpoint.id

    caplog.set_level(logging.INFO)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{http_port}/nexus/endpoints/{endpoint}/services/{service_name}/{operation_name}",
                json={"value": "test_log"},
                headers={
                    "Content-Type": "application/json",
                    "Test-Log-Header": "test-log-header-value",
                },
            )
            assert response.is_success
            response.raise_for_status()
            output_json = response.json()
            assert output_json == {"value": "logged: test_log"}

    record = next(
        (
            record
            for record in caplog.records
            if record.name == "temporalio.nexus"
            and record.getMessage() == "Logging from handler"
        ),
        None,
    )
    assert record is not None, "Expected log message not found"
    assert record.levelname == "INFO"
    assert getattr(record, "input_value", None) == "test_log"
    assert getattr(record, "service", None) == service_name
    assert getattr(record, "operation", None) == operation_name
