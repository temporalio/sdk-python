# AWS Integration for Temporal Python SDK

> ⚠️ **This package is currently at an experimental release stage.** ⚠️

This package provides AWS integrations for the Temporal Python SDK, including an Amazon S3 driver for [external storage](../../../README.md#external-storage).

## S3 Driver

`S3StorageDriver` stores and retrieves Temporal payloads in Amazon S3. It accepts any `S3StorageDriverClient` implementation and a `bucket` — either a static name or a callable for dynamic per-payload selection.

### Using the built-in aioboto3 client

The SDK ships with an [`aioboto3`](https://github.com/terrycain/aioboto3)-based client. Install the extra to pull in its dependencies:

    python -m pip install "temporalio[aioboto3]"

```python
import aioboto3
import dataclasses
from temporalio.client import Client
from temporalio.contrib.aws.s3driver import S3StorageDriver
from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client
from temporalio.converter import DataConverter, ExternalStorage

session = aioboto3.Session()
# To see how to set credentials and region via environment, config objects, or configuration files, 
# see:
# https://docs.aws.amazon.com/boto3/latest/guide/configuration.html
async with session.client("s3") as s3_client:
    driver = S3StorageDriver(
        client=new_aioboto3_client(s3_client),
        bucket="my-temporal-payloads",
    )

    client = await Client.connect(
        "localhost:7233",
        data_converter=dataclasses.replace(
            DataConverter.default,
            external_storage=ExternalStorage(drivers=[driver]),
        ),
    )
```

### Custom S3 client implementations

To use a different S3 library, subclass `S3StorageDriverClient` and implement `put_object`, `get_object`, and `object_exists`. The ABC has no external dependencies, so no AWS packages are required to import it.

```python
from temporalio.contrib.aws.s3driver import S3StorageDriverClient

class MyS3Client(S3StorageDriverClient):
    async def put_object(self, *, bucket: str, key: str, data: bytes) -> None: ...
    async def object_exists(self, *, bucket: str, key: str) -> bool: ...
    async def get_object(self, *, bucket: str, key: str) -> bytes: ...

driver = S3StorageDriver(client=MyS3Client(), bucket="my-temporal-payloads")
```

### Key structure

Payloads are stored under content-addressable keys derived from a SHA-256 hash of the serialized payload bytes, segmented by namespace and workflow/activity identifiers when serialization context is available, e.g.:

    v0/ns/my-namespace/wfi/my-workflow-id/d/sha256/<hash>

### Notes

* Any driver used to store payloads must also be configured on the component that retrieves them. If the client stores workflow inputs using this driver, the worker must include it in its `ExternalStorage.drivers` list to retrieve them.
* The target S3 bucket must already exist; the driver will not create it.
* Identical serialized bytes within the same namespace and workflow (or activity) share the same S3 object — the key is content-addressable within that scope. The same bytes used across different workflows or namespaces produce distinct S3 objects because the key includes the namespace and workflow/activity identifiers.
* Only payloads at or above `ExternalStorage.payload_size_threshold` (default: 256 KiB) are offloaded; smaller payloads are stored inline. Set `ExternalStorage.payload_size_threshold` to `0` to offload every payload regardless of size.
* `S3StorageDriver.max_payload_size` (default: 50 MiB) sets a hard upper limit on the serialized size of any single payload. A `ValueError` is raised at store time if a payload exceeds this limit. Increase it if your workflows produce payloads larger than 50 MiB.
* Override `S3StorageDriver.driver_name` only when registering multiple `S3StorageDriver` instances with distinct configurations under the same `ExternalStorage.drivers` list.

### Dynamic Bucket Selection

To select the S3 bucket per payload, pass a callable as `bucket`:

```python
from temporalio.contrib.aws.s3driver import S3StorageDriver
from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client

driver = S3StorageDriver(
    client=new_aioboto3_client(s3_client),
    bucket=lambda context, payload: (
        "large-payloads" if payload.ByteSize() > 10 * 1024 * 1024 else "small-payloads"
    ),
)
```

### Required IAM permissions

The AWS credentials used by your S3 client must have the following S3 permissions on the target bucket and its objects:

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:PutObject",
    "s3:GetObject"
  ],
  "Resource": "arn:aws:s3:::my-temporal-payloads/*"
}
```

`s3:PutObject` is required by components that store payloads (typically the Temporal client and worker sending workflow/activity inputs), and `s3:GetObject` is required by components that retrieve them (typically workers and clients reading results). Components that only retrieve payloads do not need `s3:PutObject`, and vice versa.
