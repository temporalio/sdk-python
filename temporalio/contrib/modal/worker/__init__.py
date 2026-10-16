"""Run Temporal workers inside Modal Functions.

The integration owns the worker lifecycle within one already-running Modal
Function invocation. Authentication and invocation between a controller and
Modal are outside this package.

Quick start::

    from datetime import timedelta

    import modal
    from temporalio.common import WorkerDeploymentVersion
    from temporalio.contrib.modal.worker import (
        MIN_FUNCTION_TIMEOUT_HEADROOM,
        ModalWorkerConfig,
        run_worker,
    )

    app = modal.App("temporal-worker")
    worker_execution_timeout = timedelta(minutes=5)
    modal_function_timeout = (
        worker_execution_timeout + MIN_FUNCTION_TIMEOUT_HEADROOM
    )

    def configure(config: ModalWorkerConfig) -> None:
        config.worker_config["task_queue"] = "my-task-queue"
        config.worker_config["workflows"] = [MyWorkflow]
        config.worker_config["activities"] = [my_activity]
        config.execution_timeout = worker_execution_timeout

    @app.function(timeout=int(modal_function_timeout.total_seconds()))
    async def temporal_worker() -> None:
        await run_worker(
            WorkerDeploymentVersion(
                deployment_name="my-service",
                build_id="v1.0",
            ),
            configure,
            modal_function_timeout=modal_function_timeout,
        )
"""

from temporalio.contrib.modal.worker._configure import ModalWorkerConfig
from temporalio.contrib.modal.worker._defaults import MIN_FUNCTION_TIMEOUT_HEADROOM
from temporalio.contrib.modal.worker._run_worker import run_worker

__all__ = [
    "MIN_FUNCTION_TIMEOUT_HEADROOM",
    "ModalWorkerConfig",
    "run_worker",
]
