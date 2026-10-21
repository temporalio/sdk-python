"""Driver and worker integration tests using a local GCS emulator."""

import asyncio
import dataclasses
import uuid
from datetime import timedelta

import pytest
from gcloud.aio.storage import Storage

from temporalio.api.common.v1 import Payload
from temporalio.contrib.gcp.gcsdriver import GCSStorageDriver
from temporalio.contrib.gcp.gcsdriver.gcloud_aio import new_gcloud_aio_client
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)
from temporalio.testing import WorkflowEnvironment
from tests.contrib.gcp.gcsdriver.workflows import EchoWorkflow, echo_activity
from tests.helpers import new_worker


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
