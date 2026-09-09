"""Tests for the Google Cloud Run worker-ID plugin."""

from __future__ import annotations

import os
import socket
from typing import cast
from unittest.mock import Mock

import pytest

from temporalio.contrib.gcp.cloud_run.worker_id import (
    GoogleCloudRunMetadata,
    WorkerIDPlugin,
)
from temporalio.service import ConnectConfig, ServiceClient


def _metadata(
    *,
    instance_id: str = "instance-1",
    name: str = "my-pool",
    revision: str = "rev-1",
) -> GoogleCloudRunMetadata:
    return GoogleCloudRunMetadata(
        instance_id=instance_id,
        name=name,
        revision=revision,
    )


def _closed_port() -> int:
    """Return a port number that nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _service_client() -> ServiceClient:
    return cast(ServiceClient, Mock(spec=ServiceClient))


# ---- Client identity ----


class TestClientIdentity:
    @pytest.mark.asyncio
    async def test_sets_identity_when_unset(self) -> None:
        plugin = WorkerIDPlugin(metadata=_metadata(instance_id="abc", revision="rev-1"))
        # ConnectConfig auto-fills identity with <pid>@<hostname> when none is given.
        config = ConnectConfig(target_host="localhost:7233")
        assert config.identity == f"{os.getpid()}@{socket.gethostname()}"
        service_client = _service_client()

        async def connect(input: ConnectConfig) -> ServiceClient:
            assert input.identity == "abc@rev-1"
            return service_client

        assert await plugin.connect_service_client(config, connect) is service_client
        assert config.identity == "abc@rev-1"

    @pytest.mark.asyncio
    async def test_preserves_caller_identity(self) -> None:
        plugin = WorkerIDPlugin(metadata=_metadata(instance_id="abc", revision="rev-1"))
        config = ConnectConfig(target_host="localhost:7233", identity="my-identity")
        service_client = _service_client()

        async def connect(input: ConnectConfig) -> ServiceClient:
            assert input.identity == "my-identity"
            return service_client

        assert await plugin.connect_service_client(config, connect) is service_client
        assert config.identity == "my-identity"


# ---- Metadata fetching / caching ----


class TestMetadataFetch:
    def test_construction_does_not_fetch(self) -> None:
        # A bad metadata URL must not raise at construction -- the fetch is lazy.
        WorkerIDPlugin(
            metadata_url=f"http://127.0.0.1:{_closed_port()}/instance/id",
            getenv={}.get,  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    async def test_connect_fails_fast_off_platform(self) -> None:
        plugin = WorkerIDPlugin(
            timeout=1.0,
            metadata_url=f"http://127.0.0.1:{_closed_port()}/instance/id",
            getenv={}.get,  # type: ignore[arg-type]
        )
        config = ConnectConfig(target_host="localhost:7233")

        async def connect(_input: ConnectConfig) -> ServiceClient:
            raise AssertionError("should not connect when metadata is unavailable")

        with pytest.raises(RuntimeError, match="metadata server"):
            await plugin.connect_service_client(config, connect)

    @pytest.mark.asyncio
    async def test_metadata_fetched_from_server_at_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = Mock(return_value=_metadata(instance_id="abc", revision="rev-1"))
        monkeypatch.setattr(
            "temporalio.contrib.gcp.cloud_run.worker_id._worker_id_plugin.get_google_cloud_run_metadata",
            fetch,
        )
        plugin = WorkerIDPlugin()
        config = ConnectConfig(target_host="localhost:7233")

        async def connect(_input: ConnectConfig) -> ServiceClient:
            return _service_client()

        await plugin.connect_service_client(config, connect)

        # Fetched from the metadata server when the client connects.
        fetch.assert_called_once()
        assert config.identity == "abc@rev-1"
