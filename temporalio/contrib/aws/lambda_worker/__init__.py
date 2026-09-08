"""A wrapper for running Temporal workers inside AWS Lambda.

A single :py:func:`run_worker` call handles the full per-invocation lifecycle: connecting to the
Temporal server, creating a worker with Lambda-tuned defaults, polling for tasks, and gracefully
shutting down before the invocation deadline.

Quick start::

    from temporalio.common import WorkerDeploymentVersion
    from temporalio.contrib.aws.lambda_worker import LambdaWorkerConfig, run_worker

    def configure(config: LambdaWorkerConfig) -> None:
        config.worker_config["task_queue"] = "my-task-queue"
        config.worker_config["workflows"] = [MyWorkflow]
        config.worker_config["activities"] = [my_activity]

    lambda_handler = run_worker(
        WorkerDeploymentVersion(
            deployment_name="my-service",
            build_id="v1.0",
        ),
        configure,
    )

Configuration
-------------
Client connection settings (address, namespace, TLS, API key) are loaded automatically from a TOML
config file and/or environment variables via :py:mod:`temporalio.envconfig`. The config file is
resolved in order:

1. ``TEMPORAL_CONFIG_FILE`` env var, if set.
2. ``temporal.toml`` in ``$LAMBDA_TASK_ROOT`` (typically ``/var/task``).
3. ``temporal.toml`` in the current working directory.

The file is optional -- if absent, only environment variables are used.

The configure callback receives a :py:class:`LambdaWorkerConfig` dataclass with fields pre-populated
with Lambda-appropriate defaults. Override any field directly in the callback. The ``task_queue``
key in ``worker_config`` is pre-populated from the ``TEMPORAL_TASK_QUEUE`` environment variable if
set.
"""

from temporalio.contrib.aws.lambda_worker._configure import LambdaWorkerConfig
from temporalio.contrib.aws.lambda_worker._run_worker import run_worker

__all__ = [
    "LambdaWorkerConfig",
    "run_worker",
]
