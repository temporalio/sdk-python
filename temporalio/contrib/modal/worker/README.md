# Modal worker

Run a Temporal Worker for the lifetime of one
[Modal Function](https://modal.com/docs/guide/functions) invocation. The
integration creates a fresh Temporal client and worker, applies conservative
serverless defaults, polls for tasks, and shuts the worker down before the
Function timeout.

## Quick start

```python
from datetime import timedelta

import modal

from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.modal.worker import ModalWorkerConfig, run_worker

from my_activities import my_activity
from my_workflows import MyWorkflow

WORKER_EXECUTION_TIMEOUT_SECONDS = 300

app = modal.App("temporal-worker")


def configure(config: ModalWorkerConfig) -> None:
    config.worker_config["task_queue"] = "my-task-queue"
    config.worker_config["workflows"] = [MyWorkflow]
    config.worker_config["activities"] = [my_activity]
    config.execution_timeout = timedelta(seconds=WORKER_EXECUTION_TIMEOUT_SECONDS)


@app.function(timeout=WORKER_EXECUTION_TIMEOUT_SECONDS + 15)
async def temporal_worker() -> None:
    await run_worker(
        WorkerDeploymentVersion(
            deployment_name="my-service",
            build_id="v1.0",
        ),
        configure,
    )
```

`run_worker` is async because Modal provides the invocation's event loop. Call
it once from a top-level Modal Function. Do not enable concurrent inputs for
that Function: every input owns one Temporal client and worker lifecycle.
Sequential container reuse is supported because every invocation constructs
fresh SDK objects.

## Timeout and shutdown

Modal Functions default to a five-minute input timeout. Modal does not expose a
remaining-time API inside a running Function, so `ModalWorkerConfig` uses an
explicit five-minute lifecycle budget and measures it from entry into
`run_worker`. The SDK budget may be shorter than the outer Function timeout; it
must not be longer. Leave enough outer timeout headroom for application code
that runs before or after `run_worker`:

```python
from datetime import timedelta

WORKER_EXECUTION_TIMEOUT_SECONDS = 120
FUNCTION_TIMEOUT_SECONDS = 135


def configure(config: ModalWorkerConfig) -> None:
    config.execution_timeout = timedelta(seconds=WORKER_EXECUTION_TIMEOUT_SECONDS)
    # Configure task queue, workflows, and activities here.


@app.function(timeout=FUNCTION_TIMEOUT_SECONDS)
async def temporal_worker() -> None:
    await run_worker(VERSION, configure)
```

The default shutdown buffer is seven seconds: five seconds for graceful worker
shutdown and two seconds for hooks. Increase it when shutdown hooks or worker
drain need more time. Modal cancellation and worker errors are propagated after
shutdown hooks run, allowing Modal to record or retry the interrupted input.

## Configuration

Client settings are loaded using `temporalio.envconfig`. The optional config
file is resolved in this order:

1. `TEMPORAL_CONFIG_FILE`, when set.
2. `temporal.toml` in the current working directory.

`TEMPORAL_TASK_QUEUE` pre-populates the worker task queue. The configure
callback may override it and any client or worker option. The callback runs once
per invocation and may be synchronous, async, an async generator, or an async
context manager.

The integration always enables Worker Deployment Versioning and always disables
eager activity execution. Its conservative defaults are:

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
| `max_cached_workflows` | 30 |
| `disable_eager_activity_execution` | Always `True` |

The default Temporal worker identity is
`<Modal Function Call ID>@<Modal container ID>`. A user-supplied client
`identity` takes precedence.

WCI may provide namespace, deployment name, build ID, task queue, and worker
lifetime as arguments when it invokes the Modal Function. Keep that invocation
signature at the application boundary: map those values into
`WorkerDeploymentVersion`, `client_connect_config`, `worker_config`, and
`execution_timeout` before calling `run_worker`. The SDK intentionally does not
define those argument names or their wire format while the WCI/Modal invocation
protocol is still evolving.

## Integration boundary

This package starts and stops a Temporal Worker after Modal has begun executing
the Function. It does not invoke Modal, authenticate WCI to Modal, poll a Modal
Function Call, or define a WCI/Modal request and response protocol. Those belong
to the controller/provider integration and may evolve independently of this
SDK-facing worker lifecycle.
