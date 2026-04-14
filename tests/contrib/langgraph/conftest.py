from asyncio import get_event_loop_policy
from collections.abc import AsyncGenerator

from pytest import fixture
from pytest_asyncio import fixture as async_fixture

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment


@fixture(scope="session")
def event_loop():
    loop = get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@async_fixture(scope="session")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local()
    yield env
    await env.shutdown()


@async_fixture
async def client(env: WorkflowEnvironment) -> Client:
    return env.client
