"""Tests that run against the Temporal Cloud Operations API."""

import os

import pytest

from temporalio.api.cloud.cloudservice.v1 import GetNamespaceRequest
from temporalio.client import CloudOperationsClient

# Skip entire module unless explicitly enabled
pytestmark = pytest.mark.skipif(
    "TEMPORAL_IS_CLOUD_TESTS" not in os.environ,
    reason="Cloud tests not enabled",
)


async def test_cloud_client_simple():
    client = await CloudOperationsClient.connect(
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
        version=os.environ["TEMPORAL_CLIENT_CLOUD_API_VERSION"],
    )
    result = await client.cloud_service.get_namespace(
        GetNamespaceRequest(namespace=os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"])
    )
    assert os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"] == result.namespace.namespace
