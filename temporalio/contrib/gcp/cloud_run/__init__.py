"""Run Temporal workers on Google Cloud Run.

Cloud Run runs a long-lived container rather than a per-invocation handler, so this is a small
metadata-driven plugin -- **not** a worker wrapper. :py:class:`WorkerIDPlugin` reads Cloud Run
instance metadata (from a worker pool or a service) and configures a normal, long-lived client and
worker: it sets the client identity from the Cloud Run instance and enables Worker Versioning with a
``PINNED`` deployment version derived from the Cloud Run revision.

For advanced or non-plugin use, :py:func:`get_google_cloud_run_metadata` returns the underlying
:py:class:`GoogleCloudRunMetadata`, whose ``worker_identity`` and ``worker_deployment_config``
properties expose the same values the plugin applies.

.. warning::
    Google Cloud Run support is experimental.

Quick start::

    import asyncio

    from temporalio.client import Client
    from temporalio.contrib.gcp.cloud_run import WorkerIDPlugin
    from temporalio.worker import Worker

    async def main() -> None:
        # Install the plugin on the client; it propagates to workers automatically.
        client = await Client.connect(
            "localhost:7233",
            plugins=[WorkerIDPlugin()],
        )

        worker = Worker(
            client,
            task_queue="my-task-queue",
            workflows=[MyWorkflow],
            activities=[my_activity],
        )
        await worker.run()

    asyncio.run(main())
"""

from temporalio.contrib.gcp.cloud_run._metadata import (
    CLOUD_RUN_METADATA_URL,
    GoogleCloudRunMetadata,
    get_google_cloud_run_metadata,
)
from temporalio.contrib.gcp.cloud_run._worker_id_plugin import WorkerIDPlugin

__all__ = [
    "CLOUD_RUN_METADATA_URL",
    "GoogleCloudRunMetadata",
    "WorkerIDPlugin",
    "get_google_cloud_run_metadata",
]
