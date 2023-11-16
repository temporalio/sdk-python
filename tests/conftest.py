import asyncio
import multiprocessing
import os
import sys
from typing import AsyncGenerator

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

# Unless specifically overridden, we expect tests to run under protobuf 4.x lib
import google.protobuf

protobuf_version = google.protobuf.__version__
if os.getenv("TEMPORAL_TEST_PROTO3"):
    assert protobuf_version.startswith(
        "3."
    ), f"Expected protobuf 3.x, got {protobuf_version}"
else:
    assert protobuf_version.startswith(
        "4."
    ), f"Expected protobuf 4.x, got {protobuf_version}"

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from tests.helpers.worker import ExternalPythonWorker, ExternalWorker

# Due to https://github.com/python/cpython/issues/77906, multiprocessing on
# macOS starting with Python 3.8 has changed from "fork" to "spawn". For
# pre-3.8, we are changing it for them.
if sys.version_info < (3, 8) and sys.platform.startswith("darwin"):
    multiprocessing.set_start_method("spawn", True)


def pytest_addoption(parser):
    parser.addoption(
        "--workflow-environment",
        default="local",
        help="Which workflow environment to use ('local', 'time-skipping', or target to existing server)",
    )


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


@pytest.fixture(scope="session")
def env_type(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--workflow-environment")


@pytest_asyncio.fixture(scope="session")
async def env(env_type: str) -> AsyncGenerator[WorkflowEnvironment, None]:
    if env_type == "local":
        env = await WorkflowEnvironment.start_local(
            dev_server_extra_args=[
                "--dynamic-config-value",
                "system.forceSearchAttributesCacheRefreshOnRead=true",
                "--dynamic-config-value",
                f"limit.historyCount.suggestContinueAsNew={CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT}",
                "--dynamic-config-value",
                "system.enableEagerWorkflowStart=true",
            ]
        )
    elif env_type == "time-skipping":
        env = await WorkflowEnvironment.start_time_skipping()
    else:
        env = WorkflowEnvironment.from_client(await Client.connect(env_type))
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def client(env: WorkflowEnvironment) -> Client:
    return env.client


@pytest_asyncio.fixture(scope="session")
async def worker(
    env: WorkflowEnvironment,
) -> AsyncGenerator[ExternalWorker, None]:
    worker = ExternalPythonWorker(env)
    yield worker
    await worker.close()


CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT = 50


@pytest.fixture
def continue_as_new_suggest_history_count() -> int:
    return CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT
