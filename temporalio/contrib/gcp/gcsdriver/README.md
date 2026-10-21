# Google Cloud Storage Driver

> ⚠️ **This package is experimental.** ⚠️

`GCSStorageDriver` stores and retrieves Temporal payloads in Google Cloud Storage.

Install the async GCS client:

```bash
python -m pip install "temporalio[gcloud-aio-storage]"
```

```python
import dataclasses

from gcloud.aio.storage import Storage

from temporalio.client import Client
from temporalio.contrib.gcp.gcsdriver import GCSStorageDriver
from temporalio.contrib.gcp.gcsdriver.gcloud_aio import new_gcloud_aio_client
from temporalio.converter import DataConverter, ExternalStorage

async with Storage() as storage:
    driver = GCSStorageDriver(
        client=new_gcloud_aio_client(storage),
        bucket="my-temporal-payloads",
    )
    client = await Client.connect(
        "localhost:7233",
        data_converter=dataclasses.replace(
            DataConverter.default,
            external_storage=ExternalStorage(drivers=[driver]),
        ),
    )
    # Run clients and workers inside this context so their storage client stays open.
```

Configure credentials, timeouts, and retries through the `Storage` client. See the
[gcloud-aio documentation](https://talkiq.github.io/gcloud-aio/) for details.
The adapter does not add retries, and interrupted uploads must restart.

## Configuration

- Configure this driver on every Temporal client and worker that stores or retrieves
  its payloads, using the same `driver_name` (default: `gcp.gcsdriver`).
- `bucket` accepts a name or a function taking `(context, payload)` and returning
  a bucket name. Downloads use the bucket recorded at upload time.
- The bucket must already exist. Use a flat namespace bucket and credentials with
  `storage.objects.get` and `storage.objects.create` permissions.
- `ExternalStorage.payload_size_threshold` defaults to 256 KiB. The driver accepts
  serialized payloads up to `max_payload_size` (default: 50 MiB).
- Retain objects for as long as workflow histories may need them, including replay.
  The driver leaves deletion and lifecycle policies to the application.

To use another async GCS client, implement `GCSStorageDriverClient` with
`object_exists`, `put_object`, and `get_object`. `put_object` must leave an existing
object unchanged.

## Object names and integrity

Objects store the full Temporal payload, including metadata. Names include the
namespace, available workflow or activity identifiers, and a SHA-256 hash:

```text
v0/ns/{namespace}/wt/{workflow-type}/wi/{workflow-id}/ri/{run-id}/d/sha256/{hex-digest}
v0/ns/{namespace}/at/{activity-type}/ai/{activity-id}/ri/{run-id}/d/sha256/{hex-digest}
v0/d/sha256/{hex-digest}
```

The last form is used when no target is available. Empty or missing fields use
`null`, including an empty namespace on a workflow or activity target. Names
preserve Unicode and escape characters that interfere with
[GCS tools](https://cloud.google.com/storage/docs/objects#naming), including `/`
and `%` within individual fields.

Names over 1,024 UTF-8 bytes fall back to
`v0/ns/{namespace}/d/sha256/{hex-digest}`, retaining the namespace for access policies.
Identical payload bytes in the same context reuse an object. Uploads never
overwrite existing objects, and downloads verify the hash before decoding.

## Testing

Run the tests without cloud credentials or Docker:

```bash
poe test -s tests/contrib/gcp/gcsdriver
```

Pytest downloads and caches [fake-gcs-server](https://github.com/fsouza/fake-gcs-server)
in `.pytest_cache`, starts it locally, and cleans up after the tests. To use a
preinstalled binary, put `fake-gcs-server` on `PATH`.
