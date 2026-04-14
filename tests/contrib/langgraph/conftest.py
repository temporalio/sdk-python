import asyncio
from collections.abc import AsyncGenerator, Iterator

import pytest

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local()
    yield env
    await env.shutdown()


@pytest.fixture
async def client(env: WorkflowEnvironment) -> Client:
    return env.client
