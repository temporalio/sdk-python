# cloud_run

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

A plugin for running [Temporal](https://temporal.io) workers on Google Cloud Run. Cloud Run runs a
long-lived container -- there is no per-invocation handler to wrap -- so this is **not** a worker
wrapper. Instead, `WorkerIDPlugin` reads Cloud Run instance metadata and configures a normal,
long-lived client and worker for you. Both Cloud Run **worker pools** and **services** are supported.

Register the plugin once when connecting the client and it:

- sets the client **identity** to a value derived from the Cloud Run instance (unless you already
  passed an `identity`), and
- configures the worker with a `WorkerDeploymentConfig` that enables Worker Versioning with a
  `PINNED` default behavior, so each Cloud Run revision is a distinct, pinned worker deployment
  version.

Client plugins automatically propagate to workers created from that client, so there is nothing to
wire up on the worker.

## Quick start

```python
import asyncio

from temporalio.client import Client
from temporalio.contrib.gcp.cloud_run import WorkerIDPlugin
from temporalio.worker import Worker

from my_workflows import MyWorkflow
from my_activities import my_activity


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

When the client connects, `WorkerIDPlugin` resolves the deployment name from `CLOUD_RUN_WORKER_POOL`
(falling back to `K_SERVICE`) and the revision from `CLOUD_RUN_REVISION` (falling back to
`K_REVISION`), then performs a single synchronous HTTP GET to the metadata server for the instance
id. The result is **cached on the plugin**, so the worker hook reuses it without another network
call. From that metadata the plugin applies:

- **Client identity** -- `<instance_id>@<revision>`, uniquely identifying this worker instance in
  Temporal tooling. It falls back to `<instance_id>@<name>`, then to just `<instance_id>`, when the
  revision or name is unavailable. An `identity` you pass to `Client.connect` always wins.
- **Worker deployment config** -- a `WorkerDeploymentConfig` whose version has `deployment_name` set
  to the Cloud Run workload name and `build_id` set to the Cloud Run revision, with
  `use_worker_versioning=True` and `default_versioning_behavior=VersioningBehavior.PINNED` (a
  per-workflow behavior takes precedence).

Because the metadata server is only reachable from within Cloud Run, connecting elsewhere **fails
fast** with a clear error rather than silently doing nothing. The plugin uses only the Python
standard library and adds no new dependencies.

## Advanced / non-plugin use

For advanced scenarios or unit tests you can bypass the metadata server by passing a pre-built
metadata object, or steer the fetch with `getenv` / `metadata_url` / `timeout`:

```python
from temporalio.contrib.gcp.cloud_run import WorkerIDPlugin, get_google_cloud_run_metadata

metadata = get_google_cloud_run_metadata()
plugin = WorkerIDPlugin(metadata=metadata)

# metadata.worker_identity and metadata.worker_deployment_config expose the same
# values the plugin applies, for use without the plugin if needed.
```
