import asyncio
from typing import AsyncGenerator, Optional

import pytest
import pytest_asyncio

import temporalio.client
import tests.helpers.server
import tests.helpers.worker


@pytest.fixture(scope="session")
def event_loop():
    # See https://github.com/pytest-dev/pytest-asyncio/issues/68
    # See https://github.com/pytest-dev/pytest-asyncio/issues/257
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def server() -> AsyncGenerator[tests.helpers.server.Server, None]:
    # TODO(cretz): More options such as our test server
    server = await tests.helpers.server.ExternalGolangServer.start()
    yield server
    await server.close()


@pytest_asyncio.fixture
async def client(server: tests.helpers.server.Server) -> temporalio.client.Client:
    return await server.new_client()


@pytest_asyncio.fixture
async def tls_client(
    server: tests.helpers.server.Server,
) -> Optional[temporalio.client.Client]:
    return await server.new_tls_client()


@pytest_asyncio.fixture(scope="session")
async def worker(
    server: tests.helpers.server.Server,
) -> AsyncGenerator[tests.helpers.worker.Worker, None]:
    worker = await tests.helpers.worker.ExternalGolangWorker.start(
        server.host_port, server.namespace
    )
    yield worker
    await worker.close()
