"""Fixtures for GCS tests against an explicitly configured local emulator."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import aiohttp
import pytest
from gcloud.aio.storage import Storage


@pytest.fixture
def gcs_bucket() -> str:
    return f"temporal-test-{uuid.uuid4()}"


@pytest.fixture
async def gcs_storage(gcs_bucket: str) -> AsyncIterator[Storage]:
    endpoint = os.environ.get("STORAGE_EMULATOR_HOST")
    if not endpoint:
        pytest.skip("Set STORAGE_EMULATOR_HOST to run GCS emulator integration tests")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{endpoint.rstrip('/')}/storage/v1/b",
            params={"project": "temporal-test"},
            json={"name": gcs_bucket},
        ) as response:
            response.raise_for_status()
    async with Storage(api_root=endpoint) as storage:
        yield storage
