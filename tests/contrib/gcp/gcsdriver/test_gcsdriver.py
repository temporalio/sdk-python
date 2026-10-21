"""Tests for the GCS storage driver and its async client adapter."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponseError
from gcloud.aio.storage import Storage

from temporalio.api.common.v1 import Payload
from temporalio.contrib.gcp.gcsdriver import GCSStorageDriver, GCSStorageDriverClient
from temporalio.contrib.gcp.gcsdriver.gcloud_aio import new_gcloud_aio_client
from temporalio.converter import (
    ExternalStorage,
    StorageDriverActivityInfo,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)


class MemoryClient(GCSStorageDriverClient):
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads = 0

    async def object_exists(self, *, bucket: str, object_name: str) -> bool:
        return (bucket, object_name) in self.objects

    async def put_object(self, *, bucket: str, object_name: str, data: bytes) -> None:
        self.uploads += 1
        self.objects.setdefault((bucket, object_name), data)

    async def get_object(self, *, bucket: str, object_name: str) -> bytes:
        return self.objects[bucket, object_name]


@pytest.fixture
def client() -> MemoryClient:
    return MemoryClient()


def payload(value: bytes = b"hello") -> Payload:
    return Payload(
        metadata={"encoding": b"binary/plain", "custom": b"\x00\xff"}, data=value
    )


def test_configuration(client: MemoryClient) -> None:
    driver = GCSStorageDriver(client, "bucket")
    assert driver.name() == driver.type() == "gcp.gcsdriver"
    assert GCSStorageDriver(client, "bucket", driver_name="archive").name() == "archive"
    for limit in (0, -1):
        with pytest.raises(ValueError, match="max_payload_size"):
            GCSStorageDriver(client, "bucket", max_payload_size=limit)


@pytest.mark.parametrize(
    "target,prefix",
    [
        (None, "v0"),
        (
            StorageDriverWorkflowInfo(
                namespace="ns", type="Workflow", id="wf", run_id="run"
            ),
            "v0/ns/ns/wt/Workflow/wi/wf/ri/run",
        ),
        (
            StorageDriverActivityInfo(namespace="ns", type="Activity", id="act"),
            "v0/ns/ns/at/Activity/ai/act/ri/null",
        ),
        (
            StorageDriverWorkflowInfo(namespace="ns"),
            "v0/ns/ns/wt/null/wi/null/ri/null",
        ),
        (
            StorageDriverWorkflowInfo(namespace="ns /%", id="日本語 😀", type="A+B=C"),
            "v0/ns/ns%20%2F%25/wt/A%2BB%3DC/wi/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%F0%9F%98%80/ri/null",
        ),
    ],
)
async def test_object_names(
    client: MemoryClient,
    target: StorageDriverWorkflowInfo | StorageDriverActivityInfo | None,
    prefix: str,
) -> None:
    original = payload()
    [claim] = await GCSStorageDriver(client, "bucket").store(
        StorageDriverStoreContext(target=target), [original]
    )
    digest = hashlib.sha256(original.SerializeToString()).hexdigest()
    assert claim.claim_data == {
        "bucket": "bucket",
        "object_name": f"{prefix}/d/sha256/{digest}",
        "hash_algorithm": "sha256",
        "hash_value": digest,
    }


async def test_round_trip_and_deduplication(client: MemoryClient) -> None:
    driver = GCSStorageDriver(client, "bucket")
    originals = [payload(), payload(b"world")]
    claims = await driver.store(StorageDriverStoreContext(), originals)
    assert await driver.store(StorageDriverStoreContext(), originals) == claims
    assert client.uploads == 2
    assert (
        await GCSStorageDriver(client, "new-bucket").retrieve(
            StorageDriverRetrieveContext(), claims
        )
        == originals
    )
    assert await driver.store(StorageDriverStoreContext(), []) == []
    assert await driver.retrieve(StorageDriverRetrieveContext(), []) == []


async def test_retrieve_canonical_claim(client: MemoryClient) -> None:
    original = payload()
    data = original.SerializeToString()
    client.objects["source-bucket", "existing/object"] = data
    claim = StorageDriverClaim(
        {
            "bucket": "source-bucket",
            "object_name": "existing/object",
            "hash_algorithm": "sha256",
            "hash_value": hashlib.sha256(data).hexdigest(),
        }
    )
    assert await GCSStorageDriver(client, "destination-bucket").retrieve(
        StorageDriverRetrieveContext(), [claim]
    ) == [original]


async def test_callable_bucket(client: MemoryClient) -> None:
    context = StorageDriverStoreContext(
        target=StorageDriverWorkflowInfo(namespace="ns")
    )

    def choose_bucket(ctx: StorageDriverStoreContext, value: Payload) -> str:
        assert ctx is context
        return value.data.decode()

    claims = await GCSStorageDriver(client, choose_bucket).store(
        context, [payload(b"first"), payload(b"second")]
    )
    assert [c.claim_data["bucket"] for c in claims] == ["first", "second"]


async def test_size_includes_metadata(client: MemoryClient) -> None:
    original = payload()
    driver = GCSStorageDriver(client, "bucket", max_payload_size=original.ByteSize())
    await driver.store(StorageDriverStoreContext(), [original])
    original.metadata["extra"] = b"too large"
    with pytest.raises(ValueError, match="max_payload_size"):
        await driver.store(StorageDriverStoreContext(), [original])
    assert client.uploads == 1


async def test_object_name_limit(client: MemoryClient) -> None:
    context = StorageDriverStoreContext(
        target=StorageDriverWorkflowInfo(namespace="ns", id="日" * 200)
    )
    with pytest.raises(ValueError, match="1024 UTF-8 bytes"):
        await GCSStorageDriver(client, "bucket").store(context, [payload()])
    assert not client.objects


@pytest.mark.parametrize(
    "field,value",
    [("hash_algorithm", "md5"), ("hash_value", ""), ("hash_algorithm", "")],
)
async def test_invalid_hash_claim(client: MemoryClient, field: str, value: str) -> None:
    driver = GCSStorageDriver(client, "bucket")
    [claim] = await driver.store(StorageDriverStoreContext(), [payload()])
    data = dict(claim.claim_data)
    data[field] = value
    with pytest.raises(ValueError, match="hash"):
        await driver.retrieve(
            StorageDriverRetrieveContext(), [StorageDriverClaim(data)]
        )


async def test_corruption(client: MemoryClient) -> None:
    driver = GCSStorageDriver(client, "bucket")
    [claim] = await driver.store(StorageDriverStoreContext(), [payload()])
    client.objects["bucket", claim.claim_data["object_name"]] = b"corrupted"
    with pytest.raises(ValueError, match="integrity check failed"):
        await driver.retrieve(StorageDriverRetrieveContext(), [claim])


async def test_external_storage_reference_and_threshold(client: MemoryClient) -> None:
    driver = GCSStorageDriver(client, "bucket")
    storage = ExternalStorage(drivers=[driver], payload_size_threshold=100)
    original = payload(b"large" * 100)
    assert await storage._store_payload(payload()) == payload()
    reference = await storage._store_payload(original)
    assert reference.metadata["encoding"] == b"json/protobuf"
    assert (
        reference.metadata["messageType"]
        == b"temporal.api.sdk.v1.ExternalStorageReference"
    )
    assert reference.external_payloads[0].size_bytes == original.ByteSize()
    assert await storage._retrieve_payload(reference) == original


@pytest.mark.parametrize("operation", ["object_exists", "put_object", "get_object"])
async def test_provider_error_context(operation: str) -> None:
    client = AsyncMock(spec=GCSStorageDriverClient)
    client.object_exists.return_value = False
    cause = ConnectionError("connection lost")
    getattr(client, operation).side_effect = cause
    driver = GCSStorageDriver(client, "bucket")
    with pytest.raises(RuntimeError, match="bucket=bucket, object_name=") as error:
        if operation == "get_object":
            await driver.retrieve(
                StorageDriverRetrieveContext(),
                [
                    StorageDriverClaim(
                        {"bucket": "bucket", "object_name": "object_name"}
                    )
                ],
            )
        else:
            await driver.store(StorageDriverStoreContext(), [payload()])
    assert error.value.__cause__ is cause


@pytest.mark.parametrize("retrieving", [False, True])
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_batch_cancellation(retrieving: bool, cancel_parent: bool) -> None:
    client = AsyncMock(spec=GCSStorageDriverClient)
    client.object_exists.return_value = False
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0
    cancelled = 0

    async def transfer(**_kwargs: object) -> None:
        nonlocal started, cancelled
        started += 1
        index = started
        if started == 2:
            both_started.set()
        try:
            await release.wait()
            if index == 1:
                raise ConnectionError("failed")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    client.get_object.side_effect = transfer
    client.put_object.side_effect = transfer
    driver = GCSStorageDriver(client, "bucket")
    task: asyncio.Task[list[Payload]] | asyncio.Task[list[StorageDriverClaim]]
    if retrieving:
        claim = StorageDriverClaim({"bucket": "bucket", "object_name": "object_name"})
        task = asyncio.create_task(
            driver.retrieve(StorageDriverRetrieveContext(), [claim, claim])
        )
    else:
        task = asyncio.create_task(
            driver.store(StorageDriverStoreContext(), [payload(), payload(b"other")])
        )
    await both_started.wait()
    if cancel_parent:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == 2
    else:
        release.set()
        with pytest.raises(RuntimeError):
            await task
        assert cancelled == 1


@pytest.mark.parametrize("retrieving", [False, True])
async def test_concurrent_transfers_preserve_order(retrieving: bool) -> None:
    client = AsyncMock(spec=GCSStorageDriverClient)
    client.object_exists.return_value = False
    originals = [payload(str(i).encode()) for i in range(3)]
    futures: list[asyncio.Future[bytes]] = [
        asyncio.get_running_loop().create_future() for _ in originals
    ]
    started = 0
    all_started = asyncio.Event()

    async def transfer(**_kwargs: object) -> bytes:
        nonlocal started
        future = futures[started]
        started += 1
        if started == len(originals):
            all_started.set()
        return await future

    client.get_object.side_effect = transfer
    client.put_object.side_effect = transfer
    driver = GCSStorageDriver(client, "bucket")
    claims = [
        StorageDriverClaim(
            {
                "bucket": "bucket",
                "object_name": str(i),
                "hash_algorithm": "sha256",
                "hash_value": hashlib.sha256(p.SerializeToString()).hexdigest(),
            }
        )
        for i, p in enumerate(originals)
    ]
    task: asyncio.Task[list[Payload]] | asyncio.Task[list[StorageDriverClaim]]
    if retrieving:
        task = asyncio.create_task(
            driver.retrieve(StorageDriverRetrieveContext(), claims)
        )
    else:
        task = asyncio.create_task(driver.store(StorageDriverStoreContext(), originals))
    await all_started.wait()
    for i in reversed(range(len(originals))):
        futures[i].set_result(originals[i].SerializeToString())
        await asyncio.sleep(0)
    if retrieving:
        assert await task == originals
    else:
        for actual, expected in zip(await task, claims, strict=True):
            assert isinstance(actual, StorageDriverClaim)
            assert actual.claim_data["hash_value"] == expected.claim_data["hash_value"]


def response_error(status: int) -> ClientResponseError:
    return ClientResponseError(MagicMock(), (), status=status)


@pytest.mark.parametrize("status", [None, 404, 403, 500])
async def test_adapter_object_exists(status: int | None) -> None:
    storage = AsyncMock(spec=Storage)
    if status:
        storage.download_metadata.side_effect = response_error(status)
    client = new_gcloud_aio_client(storage)
    if status in (403, 500):
        with pytest.raises(ClientResponseError):
            await client.object_exists(bucket="bucket", object_name="object_name")
    else:
        assert await client.object_exists(
            bucket="bucket", object_name="object_name"
        ) is (status is None)
    storage.download_metadata.assert_awaited_once_with("bucket", "object_name")


@pytest.mark.parametrize("status", [None, 412, 403, 404, 500])
async def test_adapter_conditional_upload(status: int | None) -> None:
    storage = AsyncMock(spec=Storage)
    if status:
        storage.upload.side_effect = response_error(status)
    storage.download_metadata.return_value = {"name": "object_name"}
    client = new_gcloud_aio_client(storage)
    if status in (403, 404, 500):
        with pytest.raises(ClientResponseError):
            await client.put_object(
                bucket="bucket", object_name="object_name", data=b"data"
            )
    else:
        await client.put_object(
            bucket="bucket", object_name="object_name", data=b"data"
        )
    storage.upload.assert_awaited_once_with(
        "bucket",
        "object_name",
        b"data",
        content_type="application/octet-stream",
        parameters={"ifGenerationMatch": "0"},
        force_resumable_upload=False,
    )
    storage.download_metadata.assert_not_awaited()


async def test_adapter_download() -> None:
    storage = AsyncMock(spec=Storage)
    storage.download.return_value = b"\x00\xff"
    assert (
        await new_gcloud_aio_client(storage).get_object(
            bucket="bucket", object_name="object_name"
        )
        == b"\x00\xff"
    )
    storage.download.assert_awaited_once_with("bucket", "object_name")
