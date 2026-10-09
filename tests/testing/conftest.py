"""Fixtures for ``tests/testing/`` test files."""

from collections.abc import AsyncGenerator

import pytest_asyncio

from temporalio.testing import WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION


@pytest_asyncio.fixture(scope="module")  # type: ignore[reportUntypedFunctionDecorator]
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    """Module-scoped time-skipping V2 dev server, shared across tests in a file."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.WorkflowTimeSkippingEnabled=true",
        ],
    ) as workflow_env:
        yield workflow_env
