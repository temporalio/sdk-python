import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment


@pytest_asyncio.fixture
async def workflow_environment() -> AsyncGenerator[WorkflowEnvironment, None]:
    """Create a workflow environment for testing."""
    env = await WorkflowEnvironment.start_time_skipping()
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def base_client(workflow_environment: WorkflowEnvironment) -> Client:
    """Base client for testing."""
    return workflow_environment.client


@pytest_asyncio.fixture
async def temporal_client(base_client: Client) -> Client:
    """Client configured with pydantic data converter for langchain tests."""
    # Create a new client with the same connection but different data converter
    return Client(
        service_client=base_client.service_client,
        namespace=base_client.namespace,
        data_converter=pydantic_data_converter,
    )


@pytest.fixture
def unique_workflow_id() -> str:
    """Generate a unique workflow ID for each test."""
    return f"test-{uuid.uuid4()}"
