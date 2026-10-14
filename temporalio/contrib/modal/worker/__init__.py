"""Run Temporal workers inside Modal Functions.

The integration owns the worker lifecycle within one already-running Modal
Function invocation. Authentication and invocation between a controller and
Modal are outside this package.

Quick start::

    import modal
    from temporalio.common import WorkerDeploymentVersion
    from temporalio.contrib.modal.worker import ModalWorkerConfig, run_worker

    app = modal.App("temporal-worker")

    def configure(config: ModalWorkerConfig) -> None:
        config.worker_config["task_queue"] = "my-task-queue"
        config.worker_config["workflows"] = [MyWorkflow]
        config.worker_config["activities"] = [my_activity]

    @app.function(timeout=315)
    async def temporal_worker() -> None:
        await run_worker(
            WorkerDeploymentVersion(
                deployment_name="my-service",
                build_id="v1.0",
            ),
            configure,
        )
"""

from temporalio.contrib.modal.worker._configure import ModalWorkerConfig
from temporalio.contrib.modal.worker._run_worker import run_worker

__all__ = [
    "ModalWorkerConfig",
    "run_worker",
]
