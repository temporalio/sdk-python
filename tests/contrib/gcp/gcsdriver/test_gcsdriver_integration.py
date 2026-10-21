"""Driver and worker integration tests using a local GCS emulator."""

import asyncio
import dataclasses
import hashlib
import uuid
from datetime import timedelta

import pytest
from aiohttp import ClientResponseError
from gcloud.aio.storage import Storage

from temporalio.api.common.v1 import Payload
from temporalio.contrib.gcp.gcsdriver import GCSStorageDriver
from temporalio.contrib.gcp.gcsdriver.gcloud_aio import new_gcloud_aio_client
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)
from temporalio.testing import WorkflowEnvironment
from tests.contrib.gcp.gcsdriver.workflows import EchoWorkflow, echo_activity
from tests.helpers import new_worker


async def test_gcs_adapter_object_operations(
    gcs_storage: Storage, gcs_bucket: str
) -> None:
    client = new_gcloud_aio_client(gcs_storage)
    name = "日本語 café 😀 /%+="
    assert not await client.object_exists(bucket=gcs_bucket, object_name=name)
    with pytest.raises(ClientResponseError) as error:
        await client.get_object(bucket=gcs_bucket, object_name=name)
    assert error.value.status == 404
    await client.put_object(bucket=gcs_bucket, object_name=name, data=b"\x00\xff")
    assert await client.object_exists(bucket=gcs_bucket, object_name=name)
    assert await client.get_object(bucket=gcs_bucket, object_name=name) == b"\x00\xff"


@pytest.mark.parametrize("operation", ["object_exists", "put_object", "get_object"])
async def test_gcs_adapter_missing_bucket(
    gcs_storage: Storage, gcs_bucket: str, operation: str
) -> None:
    client = new_gcloud_aio_client(gcs_storage)
    missing_bucket = f"{gcs_bucket}-missing"
    if operation == "object_exists":
        assert not await client.object_exists(
            bucket=missing_bucket, object_name="object"
        )
    else:
        with pytest.raises(ClientResponseError) as error:
            if operation == "put_object":
                await client.put_object(
                    bucket=missing_bucket, object_name="object", data=b"data"
                )
            else:
                await client.get_object(bucket=missing_bucket, object_name="object")
        assert error.value.status == 404


async def test_gcs_adapter_concurrent_creation(
    gcs_storage: Storage, gcs_bucket: str
) -> None:
    client = new_gcloud_aio_client(gcs_storage)
    name = "concurrent-object"
    candidates = [f"writer-{i}".encode() for i in range(5)]
    await asyncio.gather(
        *(
            client.put_object(bucket=gcs_bucket, object_name=name, data=data)
            for data in candidates
        )
    )
    stored = await client.get_object(bucket=gcs_bucket, object_name=name)
    assert stored in candidates
    before = await gcs_storage.download_metadata(gcs_bucket, name)
    await client.put_object(bucket=gcs_bucket, object_name=name, data=b"replacement")
    after = await gcs_storage.download_metadata(gcs_bucket, name)
    assert after["generation"] == before["generation"]
    assert await client.get_object(bucket=gcs_bucket, object_name=name) == stored


async def test_gcs_retrieve_canonical_claim(
    gcs_storage: Storage, gcs_bucket: str
) -> None:
    original = Payload(metadata={"encoding": b"binary/plain"}, data=b"external-writer")
    data = original.SerializeToString()
    name = "existing/日本語 object"
    await gcs_storage.upload(gcs_bucket, name, data, force_resumable_upload=False)
    claim = StorageDriverClaim(
        {
            "bucket": gcs_bucket,
            "object_name": name,
            "hash_algorithm": "sha256",
            "hash_value": hashlib.sha256(data).hexdigest(),
        }
    )
    driver = GCSStorageDriver(new_gcloud_aio_client(gcs_storage), "unused-bucket")
    assert await driver.retrieve(StorageDriverRetrieveContext(), [claim]) == [original]


async def test_gcs_long_object_name_round_trip(
    gcs_storage: Storage, gcs_bucket: str
) -> None:
    driver = GCSStorageDriver(new_gcloud_aio_client(gcs_storage), gcs_bucket)
    original = Payload(data=b"long-identity")
    [claim] = await driver.store(
        StorageDriverStoreContext(
            target=StorageDriverWorkflowInfo(namespace="ns /%", id="日" * 400)
        ),
        [original],
    )
    assert claim.claim_data["object_name"].startswith("v0/ns/ns %2F%25/d/sha256/")
    assert await driver.retrieve(StorageDriverRetrieveContext(), [claim]) == [original]


@pytest.mark.parametrize("size", [0, 6 * 1024 * 1024])
async def test_gcs_emulator_round_trip(
    gcs_storage: Storage, gcs_bucket: str, size: int
) -> None:
    client = new_gcloud_aio_client(gcs_storage)
    driver = GCSStorageDriver(client, gcs_bucket)
    original = Payload(
        metadata={"encoding": b"binary/plain", "custom": b"\x00\xff"}, data=b"x" * size
    )
    context = StorageDriverStoreContext(
        target=StorageDriverWorkflowInfo(
            namespace="ns", id="日本語 /%+", type="Workflow"
        )
    )
    claims = await driver.store(context, [original])
    assert await driver.store(context, [original]) == claims
    assert await driver.retrieve(StorageDriverRetrieveContext(), claims) == [original]
    object_name = claims[0].claim_data["object_name"]
    replacement = b"must not overwrite" + original.data
    await asyncio.gather(
        client.put_object(bucket=gcs_bucket, object_name=object_name, data=replacement),
        client.put_object(bucket=gcs_bucket, object_name=object_name, data=replacement),
    )
    assert (
        await client.get_object(bucket=gcs_bucket, object_name=object_name)
        == original.SerializeToString()
    )


async def test_gcs_workflow_round_trip(
    gcs_storage: Storage, gcs_bucket: str, env: WorkflowEnvironment
) -> None:
    driver = GCSStorageDriver(new_gcloud_aio_client(gcs_storage), gcs_bucket)
    client = await env.connect_client(
        data_converter=dataclasses.replace(
            DataConverter.default,
            external_storage=ExternalStorage(
                drivers=[driver], payload_size_threshold=256
            ),
        )
    )
    value = "large workflow input 日本語" * 100
    workflow_id = str(uuid.uuid4())
    async with new_worker(client, EchoWorkflow, activities=[echo_activity]) as worker:
        assert (
            await client.execute_workflow(
                EchoWorkflow.run,
                value,
                id=workflow_id,
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            == value
        )
    objects = await gcs_storage.list_objects(gcs_bucket)
    keys = [item["name"] for item in objects["items"]]
    assert len(keys) == 2
    assert all(
        f"/wt/EchoWorkflow/wi/{workflow_id}/ri/" in object_name for object_name in keys
    )
    assert sum("/ri/null/" in object_name for object_name in keys) == 1
