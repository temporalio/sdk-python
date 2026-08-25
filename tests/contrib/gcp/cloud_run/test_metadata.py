"""Tests for temporalio.contrib.gcp.cloud_run."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.contrib.gcp.cloud_run import (
    GoogleCloudRunMetadata,
    get_google_cloud_run_metadata,
)


def _metadata(
    *,
    instance_id: str = "instance-1",
    name: str = "",
    revision: str = "",
) -> GoogleCloudRunMetadata:
    return GoogleCloudRunMetadata(
        instance_id=instance_id,
        name=name,
        revision=revision,
    )


# ---- Local metadata-server fixture ----


class _MetadataServer(HTTPServer):
    """In-process stand-in for the Cloud Run metadata server.

    Records the headers and path of the last request and serves a configurable
    status and body so tests can assert on both the request and the response.
    """

    url: str = ""
    response_status: int = 200
    response_body: str = "instance-1"
    received_path: str | None = None
    received_headers: Message[str, str] | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        server: _MetadataServer = self.server  # type: ignore[assignment]
        server.received_path = self.path
        server.received_headers = self.headers
        body = server.response_body.encode("utf-8")
        self.send_response(server.response_status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default stderr request logging. The parameter is named
        # ``format`` to match BaseHTTPRequestHandler.log_message.
        pass


@pytest.fixture
def metadata_server() -> Iterator[_MetadataServer]:
    server = _MetadataServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    server.url = f"http://127.0.0.1:{port}/computeMetadata/v1/instance/id"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_port() -> int:
    """Return a port number that nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---- Environment precedence ----


class TestEnvPrecedence:
    def test_worker_pool_wins_over_service(
        self, metadata_server: _MetadataServer
    ) -> None:
        env = {"CLOUD_RUN_WORKER_POOL": "my-pool", "K_SERVICE": "my-service"}
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv=env.get,  # type: ignore[arg-type]
        )
        assert metadata.name == "my-pool"

    def test_service_used_when_pool_absent(
        self, metadata_server: _MetadataServer
    ) -> None:
        env = {"K_SERVICE": "my-service"}
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv=env.get,  # type: ignore[arg-type]
        )
        assert metadata.name == "my-service"

    def test_name_empty_when_neither_set(
        self, metadata_server: _MetadataServer
    ) -> None:
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv={}.get,  # type: ignore[arg-type]
        )
        assert metadata.name == ""

    def test_cloud_run_revision_wins_over_k_revision(
        self, metadata_server: _MetadataServer
    ) -> None:
        env = {"CLOUD_RUN_REVISION": "rev-cr", "K_REVISION": "rev-k"}
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv=env.get,  # type: ignore[arg-type]
        )
        assert metadata.revision == "rev-cr"

    def test_k_revision_used_when_cloud_run_revision_absent(
        self, metadata_server: _MetadataServer
    ) -> None:
        env = {"K_REVISION": "rev-k"}
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv=env.get,  # type: ignore[arg-type]
        )
        assert metadata.revision == "rev-k"

    def test_revision_empty_when_neither_set(
        self, metadata_server: _MetadataServer
    ) -> None:
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv={}.get,  # type: ignore[arg-type]
        )
        assert metadata.revision == ""


# ---- Worker identity ----


class TestWorkerIdentity:
    def test_identity_uses_revision(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="rev-1")
        assert metadata.worker_identity == "abc@rev-1"

    def test_identity_falls_back_to_name(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="")
        assert metadata.worker_identity == "abc@my-pool"

    def test_identity_falls_back_to_instance_id(self) -> None:
        metadata = _metadata(instance_id="abc", name="", revision="")
        assert metadata.worker_identity == "abc"


# ---- Worker deployment version ----


class TestWorkerDeploymentVersion:
    def test_version_from_name_and_revision(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="rev-1")
        assert metadata.worker_deployment_version == WorkerDeploymentVersion(
            deployment_name="my-pool",
            build_id="rev-1",
        )

    def test_version_errors_when_name_empty(self) -> None:
        metadata = _metadata(instance_id="abc", name="", revision="rev-1")
        with pytest.raises(ValueError, match="deployment name"):
            _ = metadata.worker_deployment_version

    def test_version_errors_when_revision_empty(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="")
        with pytest.raises(ValueError, match="revision"):
            _ = metadata.worker_deployment_version


# ---- Worker deployment config ----


class TestWorkerDeploymentConfig:
    def test_config_enables_pinned_versioning(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="rev-1")
        config = metadata.worker_deployment_config
        assert config.use_worker_versioning is True
        assert config.default_versioning_behavior == VersioningBehavior.PINNED
        assert config.version == WorkerDeploymentVersion(
            deployment_name="my-pool",
            build_id="rev-1",
        )

    def test_config_errors_when_name_empty(self) -> None:
        metadata = _metadata(instance_id="abc", name="", revision="rev-1")
        with pytest.raises(ValueError, match="deployment name"):
            _ = metadata.worker_deployment_config

    def test_config_errors_when_revision_empty(self) -> None:
        metadata = _metadata(instance_id="abc", name="my-pool", revision="")
        with pytest.raises(ValueError, match="revision"):
            _ = metadata.worker_deployment_config


# ---- HTTP fetch ----


class TestHttpFetch:
    def test_sends_metadata_flavor_header_and_trims_body(
        self, metadata_server: _MetadataServer
    ) -> None:
        metadata_server.response_body = "  instance-xyz\n"
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv={}.get,  # type: ignore[arg-type]
        )
        assert metadata.instance_id == "instance-xyz"
        assert metadata_server.received_headers is not None
        assert metadata_server.received_headers.get("Metadata-Flavor") == "Google"
        assert metadata_server.received_path == "/computeMetadata/v1/instance/id"

    def test_errors_on_non_200(self, metadata_server: _MetadataServer) -> None:
        metadata_server.response_status = 500
        metadata_server.response_body = "boom"
        with pytest.raises(RuntimeError, match="metadata server"):
            get_google_cloud_run_metadata(
                metadata_url=metadata_server.url,
                getenv={}.get,  # type: ignore[arg-type]
            )

    def test_errors_when_unreachable(self) -> None:
        url = f"http://127.0.0.1:{_closed_port()}/computeMetadata/v1/instance/id"
        with pytest.raises(RuntimeError, match="metadata server"):
            get_google_cloud_run_metadata(
                metadata_url=url,
                timeout=1.0,
                getenv={}.get,  # type: ignore[arg-type]
            )

    def test_end_to_end_from_env_and_server(
        self, metadata_server: _MetadataServer
    ) -> None:
        metadata_server.response_body = "instance-42"
        env = {"CLOUD_RUN_WORKER_POOL": "my-pool", "CLOUD_RUN_REVISION": "rev-7"}
        metadata = get_google_cloud_run_metadata(
            metadata_url=metadata_server.url,
            getenv=env.get,  # type: ignore[arg-type]
        )
        assert metadata == GoogleCloudRunMetadata(
            instance_id="instance-42",
            name="my-pool",
            revision="rev-7",
        )
        assert metadata.worker_identity == "instance-42@rev-7"
        assert metadata.worker_deployment_version == WorkerDeploymentVersion(
            deployment_name="my-pool",
            build_id="rev-7",
        )
