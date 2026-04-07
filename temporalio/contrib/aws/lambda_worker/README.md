# lambda_worker

A wrapper for running [Temporal](https://temporal.io) workers inside AWS Lambda. A single
`run_worker` call handles the full per-invocation lifecycle: connecting to the Temporal server,
creating a worker with Lambda-tuned defaults, polling for tasks, and gracefully shutting down before
the invocation deadline.

## Quick start

```python
# handler.py
from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.aws.lambda_worker import LambdaWorkerConfig, run_worker

from my_workflows import MyWorkflow
from my_activities import my_activity


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
```

## Configuration

Client connection settings (address, namespace, TLS, API key) are loaded
automatically from a TOML config file and/or environment variables via
`temporalio.envconfig`. The config file is resolved in order:

1. `TEMPORAL_CONFIG_FILE` env var, if set.
2. `temporal.toml` in `$LAMBDA_TASK_ROOT` (typically `/var/task`).
3. `temporal.toml` in the current working directory.

The file is optional -- if absent, only environment variables are used.

The configure callback receives a `LambdaWorkerConfig` dataclass with fields
pre-populated with Lambda-appropriate defaults. Override any field directly in
the callback. The `task_queue` key in `worker_config` is pre-populated from the
`TEMPORAL_TASK_QUEUE` environment variable if set.

## Lambda-tuned worker defaults

The package applies conservative concurrency limits suited to Lambda's resource
constraints:

| Setting | Default |
| --- | --- |
| `max_concurrent_activities` | 2 |
| `max_concurrent_workflow_tasks` | 10 |
| `max_concurrent_local_activities` | 2 |
| `max_concurrent_nexus_tasks` | 5 |
| `workflow_task_poller_behavior` | `SimpleMaximum(2)` |
| `activity_task_poller_behavior` | `SimpleMaximum(1)` |
| `nexus_task_poller_behavior` | `SimpleMaximum(1)` |
| `graceful_shutdown_timeout` | 5 seconds |
| `max_cached_workflows` | 100 |
| `disable_eager_activity_execution` | Always `True` |

Worker Deployment Versioning is always enabled.

## Observability

Metrics and tracing are opt-in. The `otel` module provides convenience helpers
for AWS Distro for OpenTelemetry (ADOT):

```python
from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.aws.lambda_worker import LambdaWorkerConfig, run_worker
from temporalio.contrib.aws.lambda_worker.otel import apply_defaults, OtelOptions


def configure(config: LambdaWorkerConfig) -> None:
    config.worker_config["task_queue"] = "my-task-queue"
    config.worker_config["workflows"] = [MyWorkflow]
    config.worker_config["activities"] = [my_activity]
    apply_defaults(config, OtelOptions())


lambda_handler = run_worker(
    WorkerDeploymentVersion(
        deployment_name="my-service",
        build_id="v1.0",
    ),
    configure,
)
```

You can also use `apply_metrics` or `apply_tracing` individually.

If you use OTEL, you can use
[ADOT](https://aws-otel.github.io/docs/getting-started/lambda/lambda-python)
(the AWS Distro For OpenTelemetry) to automatically integrate with AWS
observability functionality. Namely, you will want to add the Lambda layer in
the aforementioned link. We'll handle setting up the SDK for you.
