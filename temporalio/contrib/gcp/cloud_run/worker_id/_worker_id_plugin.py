"""Plugin applying Google Cloud Run worker defaults to a Temporal client and worker."""

from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable

import temporalio.plugin
from temporalio.contrib.gcp.cloud_run.worker_id._metadata import (
    CLOUD_RUN_METADATA_URL,
    GoogleCloudRunMetadata,
    get_google_cloud_run_metadata,
)
from temporalio.service import ConnectConfig, ServiceClient
from temporalio.worker import WorkerConfig


class WorkerIDPlugin(temporalio.plugin.SimplePlugin):
    """Configure a Temporal client and worker from Google Cloud Run instance metadata.

    Install this plugin once when connecting the client; it automatically
    propagates to workers created from that client. It sets the client
    **identity** to a value derived from the Cloud Run instance (unless the caller
    already provided one) and configures the worker with a
    :py:class:`temporalio.worker.WorkerDeploymentConfig` that enables Worker
    Versioning with a ``PINNED`` default behavior, so each Cloud Run revision is a
    distinct, pinned worker deployment version. Both Cloud Run worker pools and
    services are supported.

    The Cloud Run instance metadata is fetched once, lazily, when the client
    connects and then cached on the plugin. If the metadata cannot be read -- which
    usually means the process is not running on a Cloud Run worker pool or service
    -- connecting fails fast with a clear error rather than silently doing nothing.

    Unit tests and advanced callers can bypass the metadata server by passing a
    pre-built ``metadata`` object, or steer the fetch with ``getenv`` /
    ``metadata_url`` / ``timeout``.

    .. warning::
        Google Cloud Run support is experimental and may change in future versions.
    """

    def __init__(
        self,
        *,
        metadata: GoogleCloudRunMetadata | None = None,
        timeout: float = 2.0,
        metadata_url: str = CLOUD_RUN_METADATA_URL,
        getenv: Callable[[str], str | None] = os.environ.get,
    ) -> None:
        """Create a Cloud Run plugin.

        Args:
            metadata: Pre-fetched Cloud Run instance metadata. When supplied, the
                plugin uses it directly and never contacts the metadata server.
                Primarily for testing and advanced use.
            timeout: Timeout, in seconds, for the request to the metadata server.
                Ignored when ``metadata`` is supplied.
            metadata_url: URL of the Cloud Run metadata server endpoint that
                returns the instance id. Ignored when ``metadata`` is supplied.
            getenv: Callable used to look up environment variables. Defaults to
                ``os.environ.get`` and exists primarily for testing. Ignored when
                ``metadata`` is supplied.
        """
        super().__init__("WorkerIDPlugin")
        self._metadata = metadata
        self._timeout = timeout
        self._metadata_url = metadata_url
        self._getenv = getenv

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        """Fetch Cloud Run metadata and set the client identity before connecting.

        The identity is only set when the caller did not provide one, so an
        explicit ``identity`` passed to :py:meth:`temporalio.client.Client.connect`
        always wins.
        """
        metadata = self._resolve_metadata()
        if not config.identity or config.identity == _default_identity():
            config.identity = metadata.worker_identity
        return await super().connect_service_client(config, next)

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """Set the worker deployment config from the cached Cloud Run metadata.

        The deployment config enables Worker Versioning with a ``PINNED`` default
        behavior, deriving the deployment name and build id from the Cloud Run
        workload name and revision.
        """
        config = super().configure_worker(config)
        config["deployment_config"] = self._resolve_metadata().worker_deployment_config
        return config

    def _resolve_metadata(self) -> GoogleCloudRunMetadata:
        """Return the cached Cloud Run metadata, fetching it once on first use."""
        if self._metadata is None:
            self._metadata = get_google_cloud_run_metadata(
                timeout=self._timeout,
                metadata_url=self._metadata_url,
                getenv=self._getenv,  # type: ignore[arg-type]
            )
        return self._metadata


def _default_identity() -> str:
    """Recreate the identity ``ConnectConfig`` auto-generates when none is given.

    :py:class:`temporalio.service.ConnectConfig` fills an unset identity with
    ``<pid>@<hostname>`` in ``__post_init__``, so by the time this plugin runs the
    identity is never literally empty. Matching that value lets the plugin tell an
    auto-generated identity (safe to replace) from one the caller chose (kept).
    """
    return f"{os.getpid()}@{socket.gethostname()}"
