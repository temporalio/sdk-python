"""Metadata helpers for running Temporal workers on Google Cloud Run.

Cloud Run runs a long-lived container rather than a per-invocation handler, so this module is a
small metadata helper -- **not** a worker wrapper. :py:func:`get_google_cloud_run_metadata` reads
Cloud Run instance metadata (from a worker pool or a service) and hands you a worker identity string
and a :py:class:`temporalio.worker.WorkerDeploymentConfig` to drop into a normal, long-lived worker.

.. warning::
    Google Cloud Run support is experimental.

Quick start::

    import asyncio

    from temporalio.client import Client
    from temporalio.contrib.gcp.cloud_run import get_google_cloud_run_metadata
    from temporalio.worker import Worker

    async def main() -> None:
        metadata = get_google_cloud_run_metadata()

        client = await Client.connect(
            "localhost:7233",
            identity=metadata.worker_identity,
        )

        worker = Worker(
            client,
            task_queue="my-task-queue",
            workflows=[MyWorkflow],
            activities=[my_activity],
            deployment_config=metadata.worker_deployment_config,
        )
        await worker.run()

    asyncio.run(main())
"""

from temporalio.contrib.gcp.cloud_run._metadata import (
    GoogleCloudRunMetadata,
    get_google_cloud_run_metadata,
)

__all__ = [
    "GoogleCloudRunMetadata",
    "get_google_cloud_run_metadata",
]
