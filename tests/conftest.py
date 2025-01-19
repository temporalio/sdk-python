import asyncio
import logging
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

# Unless specifically overridden, we expect tests to run under protobuf 4.x/5.x lib
import google.protobuf

protobuf_version = google.protobuf.__version__
if os.getenv("TEMPORAL_TEST_PROTO3"):
    assert protobuf_version.startswith(
        "3."
    ), f"Expected protobuf 3.x, got {protobuf_version}"
else:
    assert protobuf_version.startswith("4.") or protobuf_version.startswith(
        "5."
    ), f"Expected protobuf 4.x/5.x, got {protobuf_version}"

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from tests.helpers.worker import ExternalPythonWorker, ExternalWorker


def pytest_addoption(parser):
    parser.addoption(
        "-E",
        "--workflow-environment",
        default="local",
        help="Which workflow environment to use ('local', 'time-skipping', or ip:port for existing server)",
    )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    try:
        loop.close()
    except TypeError:
        # In 3.9 tests, loop closing fails for an unclear reason, but not in
        # 3.13 tests
        if sys.version_info >= (3, 10):
            raise
    finally:
        # In 3.9 tests, the pytest-asyncio library finalizer that creates a new
        # event loop fails, but not in 3.13 tests. So for now we will make a new
        # policy that does not create the loop.
        if sys.version_info < (3, 10):
            asyncio.set_event_loop_policy(
                NoEventLoopPolicy(asyncio.get_event_loop_policy())
            )


class NoEventLoopPolicy(asyncio.AbstractEventLoopPolicy):
    def __init__(self, underlying: asyncio.AbstractEventLoopPolicy):
        super().__init__()
        self._underlying = underlying

    def get_event_loop(self):
        return self._underlying.get_event_loop()

    def set_event_loop(self, loop):
        return self._underlying.set_event_loop(loop)

    def new_event_loop(self):
        return None

    def get_child_watcher(self):
        return self._underlying.get_child_watcher()

    def set_child_watcher(self, watcher):
        return self._underlying.set_child_watcher(watcher)


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
                "--dynamic-config-value",
                "frontend.enableExecuteMultiOperation=true",
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


# There is an issue on Windows 3.9 tests in GitHub actions where even though all
# tests pass, an unclear outer area is killing the process with a bad exit code.
# This windows-only hook forcefully kills the process as success when the exit
# code from pytest is a success.
if sys.version_info < (3, 10) and sys.platform == "win32":

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_cmdline_main(config):
        result = yield
        if result.get_result() == 0:
            os._exit(0)
        return result.get_result()


CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT = 50


@pytest.fixture
def continue_as_new_suggest_history_count() -> int:
    return CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT
