"""Run Temporal workers on Google Cloud Run.

:py:class:`WorkerIDPlugin` reads Cloud Run instance metadata (from a worker pool or a service) and
sets the client identity from the Cloud Run instance.

Quick start::

    import asyncio

    from temporalio.client import Client
    from temporalio.contrib.gcp.cloud_run.worker_id import WorkerIDPlugin
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

from temporalio.contrib.gcp.cloud_run.worker_id._metadata import (
    CLOUD_RUN_METADATA_URL,
    GoogleCloudRunMetadata,
    get_google_cloud_run_metadata,
)
from temporalio.contrib.gcp.cloud_run.worker_id._worker_id_plugin import WorkerIDPlugin

__all__ = [
    "CLOUD_RUN_METADATA_URL",
    "GoogleCloudRunMetadata",
    "WorkerIDPlugin",
    "get_google_cloud_run_metadata",
]
