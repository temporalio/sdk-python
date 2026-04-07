"""Shared fixtures for S3 storage driver tests."""

from __future__ import annotations

import socket
import urllib.request
from collections.abc import AsyncIterator, Iterator

import aioboto3
import pytest
from types_aiobotocore_s3.client import S3Client

from temporalio.contrib.aws.s3driver import S3StorageDriverClient
from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client

BUCKET = "test-bucket"
REGION = "us-east-1"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def moto_server_url() -> Iterator[str]:
    """Start a moto S3 server for the test session and yield its base URL."""
    port = _find_free_port()
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=port)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
async def aioboto3_client(moto_server_url: str) -> AsyncIterator[S3Client]:
    """Yield an aioboto3 S3 client pointed at the moto server.

    Resets all moto state before each test to guarantee isolation, then
    pre-creates the standard test bucket.
    """
    urllib.request.urlopen(
        urllib.request.Request(
            f"{moto_server_url}/moto-api/reset", method="POST", data=b""
        )
    )
    session = aioboto3.Session()
    async with session.client(
        "s3",
        region_name=REGION,
        endpoint_url=moto_server_url,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as client:
        await client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def driver_client(aioboto3_client: S3Client) -> S3StorageDriverClient:
    """Wrap the aioboto3 S3 client in an S3StorageDriverClient adapter."""
    return new_aioboto3_client(aioboto3_client)
