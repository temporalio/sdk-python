# cloud_run

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

A metadata helper for running [Temporal](https://temporal.io) workers on Google Cloud Run.
Cloud Run runs a long-lived container -- there is no per-invocation handler to wrap -- so this is
**not** a worker wrapper. Instead, `get_google_cloud_run_metadata` reads Cloud Run instance metadata
and hands you a worker identity string and a `WorkerDeploymentConfig` to drop into your normal,
long-lived worker. Both Cloud Run **worker pools** and **services** are supported.

## Quick start

```python
import asyncio

from temporalio.client import Client
from temporalio.contrib.gcp.cloud_run import get_google_cloud_run_metadata
from temporalio.worker import Worker

from my_workflows import MyWorkflow
from my_activities import my_activity


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


if __name__ == "__main__":
    asyncio.run(main())
```

## How it works

Cloud Run exposes workload metadata through environment variables and a metadata server:

- **Worker pools** get `CLOUD_RUN_WORKER_POOL` and `CLOUD_RUN_REVISION` (and no `K_*` variables).
- **Services** get `K_SERVICE`, `K_REVISION`, and `K_CONFIGURATION` (and no `CLOUD_RUN_*` variables).

The unique instance id is not available as an environment variable on either; it is only exposed by
the
[Cloud Run metadata server](https://cloud.google.com/run/docs/container-contract#metadata-server)
at `http://metadata.google.internal/computeMetadata/v1/instance/id`, which requires the
`Metadata-Flavor: Google` request header.

`get_google_cloud_run_metadata` resolves the deployment name from `CLOUD_RUN_WORKER_POOL` (falling
back to `K_SERVICE`) and the revision from `CLOUD_RUN_REVISION` (falling back to `K_REVISION`), then
performs a single synchronous HTTP GET to the metadata server for the instance id. It returns a
`GoogleCloudRunMetadata` with these conveniences:

- `worker_identity` -- `<instance_id>@<revision>`, uniquely identifying this worker instance in
  Temporal tooling. It falls back to `<instance_id>@<name>`, then to just `<instance_id>`, when the
  revision or name is unavailable.
- `worker_deployment_version` -- a `WorkerDeploymentVersion` whose `deployment_name` is the Cloud
  Run workload name and whose `build_id` is the Cloud Run revision, for use with Worker Versioning.
- `worker_deployment_config` -- a `WorkerDeploymentConfig` wrapping that version with
  `use_worker_versioning=True` and `default_versioning_behavior=VersioningBehavior.PINNED` (a
  per-workflow behavior takes precedence), ready to pass to `Worker(..., deployment_config=...)`.

Because the metadata server is only reachable from within Cloud Run, calling this helper elsewhere
raises a clear error. It uses only the Python standard library and adds no new dependencies.
