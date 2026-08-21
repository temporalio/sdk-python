"""Read Google Cloud Run instance metadata for Temporal worker configuration.

Cloud Run runs a long-lived container rather than a per-invocation handler, so this module is a
small metadata helper -- not a worker wrapper. It derives a worker identity and a
:py:class:`temporalio.common.WorkerDeploymentVersion` from Cloud Run instance metadata for use with
a normal, long-lived worker. Both Cloud Run worker pools and services are supported.

.. warning::
    Google Cloud Run support is experimental.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import temporalio.common

if TYPE_CHECKING:
    import temporalio.worker


@dataclass(frozen=True)
class GoogleCloudRunMetadata:
    """Identifying metadata for the current Google Cloud Run instance.

    Both Cloud Run worker pools and services are supported. Worker pools expose
    ``CLOUD_RUN_WORKER_POOL`` and ``CLOUD_RUN_REVISION``; services expose ``K_SERVICE`` and
    ``K_REVISION``.

    Attributes:
        instance_id: Unique id of this Cloud Run container instance, read from the Cloud Run
            metadata server.
        name: Deployment name of this Cloud Run workload -- the worker pool name
            (``CLOUD_RUN_WORKER_POOL``) or, for a service, the service name (``K_SERVICE``). May be
            empty when the process is not running on Cloud Run.
        revision: Cloud Run revision name (``CLOUD_RUN_REVISION`` for worker pools or ``K_REVISION``
            for services). May be empty when the process is not running on Cloud Run.
    """

    instance_id: str
    name: str
    revision: str

    @property
    def worker_identity(self) -> str:
        """Worker identity string uniquely identifying this Cloud Run instance.

        The format is ``<instance_id>@<revision>``. When the revision is empty the deployment name
        is used instead (``<instance_id>@<name>``), and when both are empty the instance id is
        returned on its own.
        """
        if self.revision:
            return f"{self.instance_id}@{self.revision}"
        if self.name:
            return f"{self.instance_id}@{self.name}"
        return self.instance_id

    @property
    def worker_deployment_version(self) -> temporalio.common.WorkerDeploymentVersion:
        """Worker Versioning deployment version derived from this instance's metadata.

        The deployment name is the Cloud Run workload name and the build id is the Cloud Run
        revision.

        Raises:
            ValueError: If either the name or the revision is empty, which usually means the process
                is not running on a Cloud Run worker pool or service.
        """
        if not self.name or not self.revision:
            raise ValueError(
                "Cannot build a WorkerDeploymentVersion without both a Cloud Run deployment name "
                "(CLOUD_RUN_WORKER_POOL or K_SERVICE) and revision (CLOUD_RUN_REVISION or "
                "K_REVISION); this process may not be running on a Cloud Run worker pool or "
                "service."
            )
        return temporalio.common.WorkerDeploymentVersion(
            deployment_name=self.name,
            build_id=self.revision,
        )

    @property
    def worker_deployment_config(self) -> temporalio.worker.WorkerDeploymentConfig:
        """Worker deployment config with Worker Versioning enabled for this instance.

        Pass this straight to :py:class:`temporalio.worker.Worker` as its ``deployment_config``.

        Raises:
            ValueError: If either the name or the revision is empty, which usually means the process
                is not running on a Cloud Run worker pool or service.
        """
        from temporalio.worker import WorkerDeploymentConfig

        return WorkerDeploymentConfig(
            version=self.worker_deployment_version,
            use_worker_versioning=True,
        )


def get_google_cloud_run_metadata(
    *,
    timeout: float = 2.0,
    metadata_url: str = "http://metadata.google.internal/computeMetadata/v1/instance/id",
    getenv: Callable[[str], str] = os.environ.get,  # type: ignore[assignment]
) -> GoogleCloudRunMetadata:
    """Read metadata identifying the current Google Cloud Run instance.

    Resolves the deployment name from ``CLOUD_RUN_WORKER_POOL`` (Cloud Run worker pools), falling
    back to ``K_SERVICE`` (Cloud Run services), and the revision from ``CLOUD_RUN_REVISION`` falling
    back to ``K_REVISION``. The unique instance id is fetched from the Cloud Run metadata server
    with a single synchronous HTTP GET. Intended to be called once at worker startup.

    Args:
        timeout: Timeout, in seconds, for the request to the metadata server.
        metadata_url: URL of the Cloud Run metadata server endpoint that returns the instance id.
        getenv: Callable used to look up environment variables. Defaults to ``os.environ.get`` and
            exists primarily for testing.

    Returns:
        A :py:class:`GoogleCloudRunMetadata` describing the current instance.

    Raises:
        RuntimeError: If the metadata server cannot be reached, which usually means the process is
            not running on a Cloud Run worker pool or service.
    """
    name = getenv("CLOUD_RUN_WORKER_POOL") or getenv("K_SERVICE") or ""
    revision = getenv("CLOUD_RUN_REVISION") or getenv("K_REVISION") or ""

    request = urllib.request.Request(
        metadata_url,
        headers={"Metadata-Flavor": "Google"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            instance_id = response.read().decode("utf-8").strip()
    except OSError as err:
        raise RuntimeError(
            f"Failed to reach the Cloud Run metadata server at {metadata_url!r}; "
            "this process may not be running on a Cloud Run worker pool or service."
        ) from err

    return GoogleCloudRunMetadata(
        instance_id=instance_id,
        name=name,
        revision=revision,
    )
