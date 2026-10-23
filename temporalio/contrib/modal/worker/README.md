# Modal worker

Run a Temporal Worker for the lifetime of one
[Modal Function](https://modal.com/docs/guide/functions) invocation. The
integration creates a fresh Temporal client and worker, applies conservative
serverless defaults, polls for tasks, and shuts the worker down before the
Function timeout.

## Quick start

Define an Activity in `activities.py`:

```python
from temporalio import activity


@activity.defn
async def greet(name: str) -> str:
    return f"Hello, {name}!"
```

Define a Workflow in `workflows.py`:

```python
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import greet


@workflow.defn
class GreetingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet,
            name,
            start_to_close_timeout=timedelta(seconds=30),
        )
```

Create the Modal Function in `worker.py`:

```python
from datetime import timedelta

import modal

from temporalio.common import WorkerDeploymentVersion
from temporalio.contrib.modal.worker import ModalWorkerConfig, run_worker

from activities import greet
from workflows import GreetingWorkflow

FUNCTION_TIMEOUT = timedelta(minutes=5)
VERSION = WorkerDeploymentVersion(
    deployment_name="my-service",
    build_id="v1.0",
)

app = modal.App("temporal-worker")


def configure(config: ModalWorkerConfig) -> None:
    config.worker_config["task_queue"] = "my-task-queue"
    config.worker_config["workflows"] = [GreetingWorkflow]
    config.worker_config["activities"] = [greet]


@app.function(timeout=int(FUNCTION_TIMEOUT.total_seconds()))
async def temporal_worker() -> None:
    await run_worker(
        VERSION,
        configure,
        modal_function_timeout=FUNCTION_TIMEOUT,
    )
```

`run_worker` is async because Modal provides the invocation's event loop. Call
it once from a top-level Modal Function. Do not enable concurrent inputs for
that Function: every input owns one Temporal client and worker lifecycle.
Sequential container reuse is supported because every invocation constructs
fresh SDK objects.

## Timeout and shutdown

[Modal Functions default to a five-minute timeout](https://modal.com/docs/guide/timeouts).
Pass the Function's configured timeout to `run_worker` as
`modal_function_timeout`, as shown above. The two values must match because
Modal does not expose the remaining invocation time to running Function code.

The SDK reserves 15 seconds of internal safety headroom. By default, the rest
is the execution budget, which includes configuration, connection, polling,
worker shutdown, and shutdown hooks.

With a five-minute Function timeout and the default five-second graceful
shutdown timeout, the lifecycle is approximately:

| Time after `run_worker` starts | Event |
| --- | --- |
| 0:00 | Configuration and connection begin. |
| 4:38 | Polling stops and worker shutdown begins. |
| 4:43 | The default graceful Activity drain period ends. |
| 4:45 | The SDK targets completion after reserving two seconds for shutdown hooks. |
| 5:00 | Modal's Function timeout is reached. |

Setup and connection time consume the polling budget, so the worker may poll
for less than 4 minutes 38 seconds. When shutdown begins, the worker stops
polling for new tasks and lets in-flight Activities finish for up to
`graceful_shutdown_timeout`. It then runs the configured shutdown hooks.

The default `shutdown_deadline_buffer` is calculated after `configure` as
`graceful_shutdown_timeout + 2 seconds`. Increasing
`graceful_shutdown_timeout` therefore moves the stop-polling time earlier
without requiring a second setting. Set `shutdown_deadline_buffer` explicitly
only when shutdown hooks or other drain work require a different reserve.
Modal's Function timeout remains the final deadline.

Worker errors and Modal cancellation propagate after shutdown hooks run, so
Modal can record or retry an interrupted input.

## Configuration

Client settings are loaded using `temporalio.envconfig`. The optional config
file is resolved in this order:

1. `TEMPORAL_CONFIG_FILE`, when set.
2. `temporal.toml` in the current working directory.

`TEMPORAL_TASK_QUEUE` pre-populates the worker task queue. The configure
callback may override it and any client or worker option. The callback runs once
per invocation and may be synchronous, async, an async generator, or an async
context manager.

`execution_timeout` defaults to the Modal Function timeout minus the SDK's
internal safety headroom. The configure callback may shorten it when an
invocation should stop earlier, but cannot extend it into that headroom.

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
