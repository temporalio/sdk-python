# Google Cloud Storage Driver

> ⚠️ **This package is experimental.** ⚠️

`GCSStorageDriver` stores and retrieves Temporal payloads in Google Cloud Storage.

Install the optional async client dependency:

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

The application owns the `Storage` client and configures credentials, request
timeouts, and retry behavior. See the [gcloud-aio documentation](https://talkiq.github.io/gcloud-aio/)
for authentication and customization. The library has no general retry policy;
applications can supply a `Storage` subclass with async retries and timeouts.
The adapter uses a single request for each upload, including payloads over 5 MiB.
Interrupted uploads must restart from the beginning. Conditional creation prevents
retries from overwriting existing objects.

## Configuration

- Configure this driver on every Temporal client and worker that stores or retrieves
  its payloads, using the same `driver_name` (default: `gcp.gcsdriver`).
- `bucket` accepts a bucket name or a callable taking `(StorageDriverStoreContext,
  Payload)` and returning a bucket name for each upload. Reads use the bucket in
  the stored claim.
- The bucket must already exist. Use a flat namespace bucket and credentials with
  `storage.objects.get` and `storage.objects.create` permissions.
- `ExternalStorage.payload_size_threshold` defaults to 256 KiB. The driver accepts
  serialized payloads up to `max_payload_size` (default: 50 MiB).
- Retain objects for as long as workflow histories may need them, including replay.
  The driver leaves deletion and lifecycle policies to the application.

To use another async GCS client, subclass `GCSStorageDriverClient` and implement
`object_exists`, `put_object`, and `get_object`. `put_object` must leave an existing
object unchanged. Importing the driver and client ABC requires no GCS dependencies.

## Object names and integrity

Objects contain the complete serialized Temporal `Payload` protobuf, including
metadata. Object names include available workflow or activity context and a
SHA-256 digest:

```text
v0/ns/{namespace}/wt/{workflow-type}/wi/{workflow-id}/ri/{run-id}/d/sha256/{hex-digest}
v0/ns/{namespace}/at/{activity-type}/ai/{activity-id}/ri/{run-id}/d/sha256/{hex-digest}
v0/d/sha256/{hex-digest}
```

The last form applies when target context is unavailable. Empty or missing identity
fields use `null`, including the namespace when a workflow or activity target is
present. Dynamic segments preserve Unicode, spaces, and safe punctuation.
Characters `# [ ] * ? : " < > | / %`, C0 controls (U+0000–U+001F), and DEL/C1
controls (U+007F–U+009F) are escaped as uppercase `%XX` per UTF-8 byte. Whole
segments `.` and `..` become `%2E` and `%2E%2E`. See the
[GCS naming guidance](https://cloud.google.com/storage/docs/objects#naming).
If a full object name exceeds 1,024 UTF-8 bytes, the driver omits the workflow or
activity identity segments and uses `v0/ns/{namespace}/d/sha256/{hex-digest}`.
This preserves namespace scope for authorization policies and deduplicates
identical serialized payloads across that namespace.

Identical serialized bytes in the same context share an object. The driver checks
existence before uploading; the built-in adapter also uses conditional creation to
prevent concurrent writers from overwriting objects. Claims contain `bucket`, `object_name`,
`hash_algorithm`, and `hash_value`. Downloads verify SHA-256 over the exact stored
bytes before parsing the protobuf. Equivalent values serialized by different SDKs
may produce different bytes and object names.

## Testing

The driver, emulator, and Temporal worker integration tests run without cloud
credentials or Docker:

```bash
poe test -s tests/contrib/gcp/gcsdriver
```

Pytest starts a local [fake-gcs-server](https://github.com/fsouza/fake-gcs-server)
process for each worker that needs it and stops the process after the session.
On first use, it downloads version 1.56.1 for the current platform, verifies the
archive's SHA-256 checksum, and caches the binary in `.pytest_cache`. Supported
platforms are Linux and macOS on AMD64/ARM64, and Windows on AMD64.

Set `FAKE_GCS_SERVER_PATH` to use an existing binary, or put `fake-gcs-server` on
`PATH`.
Each test creates a unique bucket and deletes its objects and bucket afterward.
