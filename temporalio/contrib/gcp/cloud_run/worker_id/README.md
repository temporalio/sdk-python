# worker_id

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

A plugin for running [Temporal](https://temporal.io) workers on Google Cloud Run. `CloudRunIDPlugin`
reads Cloud Run instance metadata and sets the client identity. Both Cloud Run **worker pools** and
**services** are supported.

Register the plugin once when connecting the client and it sets the client **identity** to a value
derived from the Cloud Run instance (unless you already passed an `identity`).

## Quick start

```python
import asyncio

from temporalio.client import Client
from temporalio.contrib.gcp.cloud_run.worker_id import CloudRunIDPlugin
from temporalio.worker import Worker

from my_workflows import MyWorkflow
from my_activities import my_activity


async def main() -> None:
    # Install the plugin on the client; it propagates to workers automatically.
    client = await Client.connect(
        "localhost:7233",
        plugins=[CloudRunIDPlugin()],
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

When the client connects, `CloudRunIDPlugin` resolves the worker pool name from
`CLOUD_RUN_WORKER_POOL` (falling back to the service name `K_SERVICE`) and the revision from
`CLOUD_RUN_REVISION` (falling back to
`K_REVISION`), then performs a single synchronous HTTP GET to the metadata server for the instance
id. From that metadata the plugin sets:

- **Client identity** -- `<instance_id>@<revision>`, uniquely identifying this worker instance in
  Temporal tooling. It falls back to `<instance_id>@<name>`, then to just `<instance_id>`, when the
  revision or name is unavailable. An `identity` you pass to `Client.connect` always wins.

The metadata server is only reachable from within Cloud Run, so connecting elsewhere raises an
error. The plugin uses only the Python standard library and adds no new dependencies.

## Advanced / non-plugin use

For advanced scenarios or unit tests you can bypass the metadata server by passing a pre-built
metadata object, or steer the fetch with `getenv` / `metadata_url` / `timeout`:

```python
from temporalio.contrib.gcp.cloud_run.worker_id import CloudRunIDPlugin, get_google_cloud_run_metadata

metadata = get_google_cloud_run_metadata()
plugin = CloudRunIDPlugin(metadata=metadata)

# metadata.worker_identity exposes the same value the plugin applies, for use
# without the plugin if needed.
```
