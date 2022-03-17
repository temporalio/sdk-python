import asyncio
import os
import sys
from typing import AsyncGenerator, Optional

import pytest
import pytest_asyncio

# If there is an integration test environment variable set, we must remove the
# first path from the sys.path so we can import the wheel instead
if os.getenv("TEMPORAL_INTEGRATION_TEST"):
    assert (
        sys.path[0] == os.getcwd()
    ), "Expected first sys.path to be the current working dir"
    sys.path.pop(0)
    # Import temporalio and confirm it is prefixed with virtual env
    import temporalio

    assert temporalio.__file__.startswith(
        sys.prefix
    ), f"Expected {temporalio.__file__} to be in {sys.prefix}"

import temporalio.client
import tests.helpers.server
import tests.helpers.worker


@pytest.fixture(scope="session")
def event_loop():
    # See https://github.com/pytest-dev/pytest-asyncio/issues/68
    # See https://github.com/pytest-dev/pytest-asyncio/issues/257
    # Also need ProactorEventLoop on older versions of Python with Windows so
    # that asyncio subprocess works properly
    if sys.version_info < (3, 8) and sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
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
