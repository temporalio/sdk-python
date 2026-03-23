"""Tests that run against Temporal Cloud."""

import multiprocessing
import os
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio

from temporalio.api.cloud.cloudservice.v1 import GetNamespaceRequest
from temporalio.client import Client, CloudOperationsClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import SharedStateManager
from tests.helpers.worker import ExternalPythonWorker, ExternalWorker

# Skip entire module if cloud env vars are missing
pytestmark = pytest.mark.skipif(
    "TEMPORAL_CLIENT_CLOUD_API_KEY" not in os.environ,
    reason="No cloud API key",
)


@pytest_asyncio.fixture(scope="module")  # type: ignore[reportUntypedFunctionDecorator]
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    client = await Client.connect(
        os.environ["TEMPORAL_CLIENT_CLOUD_TARGET"],
        namespace=os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"],
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
    )
    env = WorkflowEnvironment.from_client(client)
    yield env
    await env.shutdown()


@pytest_asyncio.fixture  # type: ignore[reportUntypedFunctionDecorator]
async def client(env: WorkflowEnvironment) -> Client:
    return env.client


@pytest_asyncio.fixture(scope="module")  # type: ignore[reportUntypedFunctionDecorator]
async def worker(
    env: WorkflowEnvironment,
) -> AsyncGenerator[ExternalWorker, None]:
    w = ExternalPythonWorker(env)
    yield w
    await w.close()


@pytest.fixture(scope="module")
def shared_state_manager() -> Iterator[SharedStateManager]:
    mp_mgr = multiprocessing.Manager()
    mgr = SharedStateManager.create_from_multiprocessing(mp_mgr)
    try:
        yield mgr
    finally:
        mp_mgr.shutdown()


# --- Cloud-specific tests ---


async def test_cloud_client_simple():
    client = await CloudOperationsClient.connect(
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
        version=os.environ["TEMPORAL_CLIENT_CLOUD_API_VERSION"],
    )
    result = await client.cloud_service.get_namespace(
        GetNamespaceRequest(namespace=os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"])
    )
    assert os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"] == result.namespace.namespace


# --- Delegated tests ---
# Import test functions to re-run them against cloud fixtures.

from tests.worker.test_activity import (  # noqa: E402
    test_activity_info,  # pyright: ignore[reportUnusedImport]  # noqa: F401
)
