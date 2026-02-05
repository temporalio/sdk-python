import asyncio
import multiprocessing.context
import os
import sys
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import SharedStateManager
from tests.helpers.worker import ExternalPythonWorker, ExternalWorker

from . import DEV_SERVER_DOWNLOAD_VERSION

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
    assert (
        protobuf_version.startswith("4.")
        or protobuf_version.startswith("5.")
        or protobuf_version.startswith("6.")
    ), f"Expected protobuf 4.x/5.x/6.x, got {protobuf_version}"


def pytest_runtest_setup(item):  # type: ignore[reportMissingParameterType]
    """Print a newline so that custom printed output starts on new line."""
    if item.config.getoption("-s"):
        print()


def pytest_addoption(parser):  # type: ignore[reportMissingParameterType]
    parser.addoption(
        "-E",
        "--workflow-environment",
        default="local",
        help="Which workflow environment to use ('local', 'time-skipping', or ip:port for existing server)",
    )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()  # type: ignore[reportDeprecated]
    yield loop
    try:
        loop.close()
    except TypeError:
        raise


class NoEventLoopPolicy(asyncio.AbstractEventLoopPolicy):  # type: ignore[name-defined]
    def __init__(self, underlying: asyncio.AbstractEventLoopPolicy):  # type: ignore[name-defined]
        super().__init__()
        self._underlying = underlying

    def get_event_loop(self):
        return self._underlying.get_event_loop()

    def set_event_loop(self, loop):  # type: ignore[reportMissingParameterType]
        return self._underlying.set_event_loop(loop)

    def new_event_loop(self):  # type: ignore[reportIncompatibleMethodOverride]
        return None

    def get_child_watcher(self):
        return self._underlying.get_child_watcher()  # type: ignore[reportDeprecated]

    def set_child_watcher(self, watcher):  # type: ignore[reportMissingParameterType]
        return self._underlying.set_child_watcher(watcher)  # type: ignore[reportDeprecated]


@pytest.fixture(scope="session")
def env_type(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--workflow-environment")  # type: ignore[reportReturnType]


@pytest_asyncio.fixture(scope="session")  # type: ignore[reportUntypedFunctionDecorator]
async def env(env_type: str) -> AsyncGenerator[WorkflowEnvironment, None]:
    if env_type == "local":
        http_port = 7243
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
                "--dynamic-config-value",
                "frontend.workerVersioningWorkflowAPIs=true",
                "--dynamic-config-value",
                "frontend.workerVersioningDataAPIs=true",
                "--dynamic-config-value",
                "system.enableDeploymentVersions=true",
                "--dynamic-config-value",
                "frontend.activityAPIsEnabled=true",
                "--dynamic-config-value",
                "component.nexusoperations.recordCancelRequestCompletionEvents=true",
                "--dynamic-config-value",
                "activity.enableStandalone=true",
                "--dynamic-config-value",
                "history.enableChasm=true",
                "--dynamic-config-value",
                "history.enableTransitionHistory=true",
                "--http-port",
                str(http_port),
            ],
            dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        )
        # TODO(nexus-preview): expose this in a more principled way
        env._http_port = http_port  # type: ignore
    elif env_type == "time-skipping":
        env = await WorkflowEnvironment.start_time_skipping()
    else:
        env = WorkflowEnvironment.from_client(await Client.connect(env_type))

    yield env
    await env.shutdown()


@pytest.fixture(scope="session")
def shared_state_manager() -> Iterator[SharedStateManager]:
    mp_mgr = multiprocessing.Manager()
    mgr = SharedStateManager.create_from_multiprocessing(mp_mgr)

    try:
        yield mgr
    finally:
        mp_mgr.shutdown()


@pytest.fixture(scope="session")
def mp_fork_ctx() -> Iterator[multiprocessing.context.BaseContext | None]:
    mp_ctx = None
    try:
        mp_ctx = multiprocessing.get_context("fork")
    except ValueError:
        pass

    try:
        yield mp_ctx
    finally:
        if mp_ctx:
            for p in mp_ctx.active_children():
                p.terminate()
                p.join()


@pytest_asyncio.fixture  # type: ignore[reportUntypedFunctionDecorator]
async def client(env: WorkflowEnvironment) -> Client:
    return env.client


@pytest_asyncio.fixture(scope="session")  # type: ignore[reportUntypedFunctionDecorator]
async def worker(
    env: WorkflowEnvironment,
) -> AsyncGenerator[ExternalWorker, None]:
    worker = ExternalPythonWorker(env)
    yield worker
    await worker.close()


# There is an issue in tests sometimes in GitHub actions where even though all tests
# pass, an unclear outer area is killing the process with a bad exit code. This
# hook forcefully kills the process as success when the exit code from pytest
# is a success.
@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_cmdline_main(config):  # type: ignore[reportMissingParameterType, reportUnusedParameter]
    result = yield
    if result.get_result() == 0:
        os._exit(0)
    return result.get_result()


CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT = 50


@pytest.fixture
def continue_as_new_suggest_history_count() -> int:
    return CONTINUE_AS_NEW_SUGGEST_HISTORY_COUNT
