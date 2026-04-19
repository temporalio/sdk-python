"""Unit tests for S3StorageDriver using moto's ThreadedMotoServer to mock AWS S3.

moto's standard mock_aws() context manager intercepts boto3/botocore via the
requests library and does not intercept aiobotocore (which aioboto3 wraps),
because aiobotocore uses aiohttp and returns coroutines where moto's mock
returns plain bytes. ThreadedMotoServer starts a real local HTTP server; the
aioboto3 client is pointed at it via endpoint_url so all API calls are
intercepted correctly.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from types_aiobotocore_s3.client import S3Client

from temporalio.api.common.v1 import Payload
from temporalio.contrib.aws.s3driver import (
    S3StorageDriver,
    S3StorageDriverClient,
)
from temporalio.converter import (
    JSONPlainPayloadConverter,
    StorageDriverActivityInfo,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)
from tests.contrib.aws.s3driver.conftest import BUCKET

_CONVERTER = JSONPlainPayloadConverter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_payload(value: str = "hello") -> Payload:
    p = _CONVERTER.to_payload(value)
    assert p is not None
    return p


def make_store_context(
    target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None = None,
) -> StorageDriverStoreContext:
    return StorageDriverStoreContext(
        target=target,
    )


def make_workflow_context(
    namespace: str = "my-namespace",
    workflow_id: str = "my-workflow",
    workflow_type: str | None = None,
    run_id: str | None = None,
) -> StorageDriverStoreContext:
    return make_store_context(
        target=StorageDriverWorkflowInfo(
            id=workflow_id, type=workflow_type, run_id=run_id, namespace=namespace
        ),
    )


def make_activity_context(
    namespace: str = "my-namespace",
    activity_id: str | None = "my-activity",
    activity_type: str | None = None,
    run_id: str | None = None,
) -> StorageDriverStoreContext:
    return make_store_context(
        target=StorageDriverActivityInfo(
            id=activity_id, type=activity_type, run_id=run_id, namespace=namespace
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class CountingDriverClient(S3StorageDriverClient):
    """S3StorageDriverClient wrapper that counts calls to each method."""

    def __init__(self, delegate: S3StorageDriverClient) -> None:
        self._delegate = delegate
        self.put_object_count = 0
        self.get_object_count = 0
        self.object_exists_count = 0

    async def put_object(self, *, bucket: str, key: str, data: bytes) -> None:
        """Delegate to wrapped client and increment put_object counter."""
        self.put_object_count += 1
        await self._delegate.put_object(bucket=bucket, key=key, data=data)

    async def object_exists(self, *, bucket: str, key: str) -> bool:
        """Delegate to wrapped client and increment object_exists counter."""
        self.object_exists_count += 1
        return await self._delegate.object_exists(bucket=bucket, key=key)

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        """Delegate to wrapped client and increment get_object counter."""
        self.get_object_count += 1
        return await self._delegate.get_object(bucket=bucket, key=key)


class FailOnceDriverClient(S3StorageDriverClient):
    """S3StorageDriverClient wrapper that fails the first call to a specified
    method and blocks subsequent calls until cancelled.

    Used to verify that the driver cancels in-flight tasks when one fails.
    """

    def __init__(
        self,
        delegate: S3StorageDriverClient,
        fail_on: str,
    ) -> None:
        self._delegate = delegate
        self._fail_on = fail_on
        self._call_count = 0
        self.cancelled: list[bool] = []

    async def _maybe_fail(self) -> None:
        self._call_count += 1
        if self._call_count == 1:
            raise ConnectionError("S3 connection lost")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.append(True)
            raise

    async def put_object(self, *, bucket: str, key: str, data: bytes) -> None:
        """Delegate or fail depending on configuration."""
        if self._fail_on == "put_object":
            await self._maybe_fail()
        await self._delegate.put_object(bucket=bucket, key=key, data=data)

    async def object_exists(self, *, bucket: str, key: str) -> bool:
        """Delegate or fail depending on configuration."""
        if self._fail_on == "object_exists":
            await self._maybe_fail()
        return await self._delegate.object_exists(bucket=bucket, key=key)

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        """Delegate or fail depending on configuration."""
        if self._fail_on == "get_object":
            await self._maybe_fail()
        return await self._delegate.get_object(bucket=bucket, key=key)


@pytest.fixture
def counting_driver_client(
    driver_client: S3StorageDriverClient,
) -> CountingDriverClient:
    """Wrap the driver client in a counting decorator."""
    return CountingDriverClient(driver_client)


# ---------------------------------------------------------------------------
# TestS3StorageDriverInit — no S3 calls; MagicMock client is sufficient
# ---------------------------------------------------------------------------


class TestS3StorageDriverInit:
    def test_default_name(self) -> None:
        driver = S3StorageDriver(
            client=MagicMock(spec=S3StorageDriverClient), bucket=BUCKET
        )
        assert driver.name() == "aws.s3driver"

    def test_custom_name(self) -> None:
        driver = S3StorageDriver(
            client=MagicMock(spec=S3StorageDriverClient),
            bucket=BUCKET,
            driver_name="my-s3",
        )
        assert driver.name() == "my-s3"

    def test_type(self) -> None:
        driver = S3StorageDriver(
            client=MagicMock(spec=S3StorageDriverClient), bucket=BUCKET
        )
        assert driver.type() == "aws.s3driver"


# ---------------------------------------------------------------------------
# TestS3StorageDriverKeyConstruction
# ---------------------------------------------------------------------------


class TestS3StorageDriverKeyConstruction:
    async def test_key_context_none(self, driver_client: S3StorageDriverClient) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        [claim] = await driver.store(make_store_context(), [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert claim.claim_data["key"] == f"v0/d/sha256/{expected_hash}"

    async def test_key_context_workflow(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(namespace="ns1", workflow_id="wf1")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/wt/null/wi/wf1/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_context_workflow_with_type_and_run_id(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(
            namespace="ns1",
            workflow_id="wf1",
            workflow_type="MyWorkflow",
            run_id="run-abc",
        )
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/wt/MyWorkflow/wi/wf1/ri/run-abc/d/sha256/{expected_hash}"
        )

    async def test_key_context_activity(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """activity target uses activity key segment."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_activity_context(namespace="ns1", activity_id="act1")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/at/null/ai/act1/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_context_activity_with_type_and_run_id(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_activity_context(
            namespace="ns1",
            activity_id="act1",
            activity_type="MyActivity",
            run_id="run-abc",
        )
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/at/MyActivity/ai/act1/ri/run-abc/d/sha256/{expected_hash}"
        )

    async def test_key_preserves_case(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(namespace="MyNamespace", workflow_id="MyWorkflow")
        [claim] = await driver.store(ctx, [payload])
        key = claim.claim_data["key"]
        assert "MyNamespace" in key
        assert "MyWorkflow" in key

    async def test_key_urlencodes_workflow_id_with_slashes(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(namespace="ns1", workflow_id="order/123/v2")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/wt/null/wi/order%2F123%2Fv2/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_urlencodes_workflow_id_with_special_chars(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(namespace="ns1", workflow_id="wf#1 &foo=bar")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/wt/null/wi/wf%231%20%26foo%3Dbar/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_urlencodes_activity_id(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_activity_context(namespace="ns1", activity_id="act/1#2")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/ns1/at/null/ai/act%2F1%232/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_urlencodes_namespace(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        ctx = make_workflow_context(namespace="my/ns#1", workflow_id="wf1")
        [claim] = await driver.store(ctx, [payload])
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert (
            claim.claim_data["key"]
            == f"v0/ns/my%2Fns%231/wt/null/wi/wf1/ri/null/d/sha256/{expected_hash}"
        )

    async def test_key_urlencoded_roundtrip(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """Payloads stored with special-char IDs can be retrieved correctly."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("special-char-roundtrip")
        ctx = make_workflow_context(namespace="ns/1", workflow_id="wf/2#3")
        [claim] = await driver.store(ctx, [payload])
        [retrieved] = await driver.retrieve(StorageDriverRetrieveContext(), [claim])
        assert retrieved == payload


# ---------------------------------------------------------------------------
# TestS3StorageDriverStoreRetrieve
# ---------------------------------------------------------------------------


class TestS3StorageDriverStoreRetrieve:
    async def test_store_returns_claim_with_bucket_key_and_hash(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload()
        [claim] = await driver.store(make_store_context(), [payload])
        assert claim.claim_data["bucket"] == BUCKET
        assert "key" in claim.claim_data
        assert claim.claim_data["hash_algorithm"] == "sha256"
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        assert claim.claim_data["hash_value"] == expected_hash

    async def test_roundtrip_single_payload(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("round-trip value")
        [claim] = await driver.store(make_store_context(), [payload])
        [retrieved] = await driver.retrieve(StorageDriverRetrieveContext(), [claim])
        assert retrieved == payload

    async def test_roundtrip_multiple_payloads(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payloads = [make_payload(f"value-{i}") for i in range(3)]
        claims = await driver.store(make_store_context(), payloads)
        retrieved = await driver.retrieve(StorageDriverRetrieveContext(), claims)
        assert retrieved == payloads

    async def test_empty_payloads_returns_empty_list(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        assert await driver.store(make_store_context(), []) == []
        assert await driver.retrieve(StorageDriverRetrieveContext(), []) == []

    async def test_roundtrip_multipart_payload(
        self, aioboto3_client: S3Client, driver_client: S3StorageDriverClient
    ) -> None:
        """Payloads above the 8 MiB multipart threshold are uploaded via multipart
        and retrieved correctly. The S3 ETag for multipart objects contains a '-'
        suffix (e.g. 'hash-2'), which we assert to confirm multipart was used."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        # Slightly above the default 8 MiB multipart_threshold
        large_payload = make_payload("x" * (9 * 1024 * 1024))
        [claim] = await driver.store(make_store_context(), [large_payload])
        [retrieved] = await driver.retrieve(StorageDriverRetrieveContext(), [claim])
        assert retrieved == large_payload
        head = await aioboto3_client.head_object(
            Bucket=BUCKET, Key=claim.claim_data["key"]
        )
        assert "-" in head["ETag"], "Expected a multipart ETag (hash-N format)"

    async def test_content_addressable_deduplication(
        self, aioboto3_client: S3Client, driver_client: S3StorageDriverClient
    ) -> None:
        """Two identical payloads produce the same S3 key; only one object is stored."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("same-value")
        claims = await driver.store(make_store_context(), [payload, payload])
        assert claims[0].claim_data["key"] == claims[1].claim_data["key"]
        response = await aioboto3_client.list_objects_v2(Bucket=BUCKET)
        assert response["KeyCount"] == 1

    async def test_skips_upload_when_key_exists(
        self, counting_driver_client: CountingDriverClient
    ) -> None:
        """When a key already exists in S3, put_object is not called again."""
        driver = S3StorageDriver(client=counting_driver_client, bucket=BUCKET)
        payload = make_payload("upload-once")

        await driver.store(make_store_context(), [payload])
        assert counting_driver_client.put_object_count == 1

        await driver.store(make_store_context(), [payload])
        assert (
            counting_driver_client.put_object_count == 1
        ), "put_object should not be called for an existing key"

    async def test_skips_upload_preserves_data(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """Storing the same payload twice returns correct data on retrieve."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("preserve-me")

        [claim1] = await driver.store(make_store_context(), [payload])
        [claim2] = await driver.store(make_store_context(), [payload])
        assert claim1 == claim2

        [retrieved] = await driver.retrieve(StorageDriverRetrieveContext(), [claim2])
        assert retrieved == payload

    async def test_retrieve_validates_hash(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """Retrieve raises RuntimeError when the hash in the claim doesn't match."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("check-integrity")
        [claim] = await driver.store(make_store_context(), [payload])

        tampered_claim = StorageDriverClaim(
            claim_data={
                **claim.claim_data,
                "hash_value": "0" * 64,
            },
        )
        with pytest.raises(
            ValueError,
            match=r"S3StorageDriver integrity check failed \[bucket=.+, key=.+\]: expected sha256:.+, got sha256:.+",
        ):
            await driver.retrieve(StorageDriverRetrieveContext(), [tampered_claim])

    async def test_retrieve_rejects_unsupported_hash_algorithm(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """Retrieve raises ValueError when the claim specifies a non-sha256 algorithm."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("unsupported-algo")
        [claim] = await driver.store(make_store_context(), [payload])

        bad_claim = StorageDriverClaim(
            claim_data={
                **claim.claim_data,
                "hash_algorithm": "md5",
            },
        )
        with pytest.raises(
            ValueError,
            match=r"S3StorageDriver unsupported hash algorithm \[bucket=.+, key=.+\]: expected sha256, got md5",
        ):
            await driver.retrieve(StorageDriverRetrieveContext(), [bad_claim])

    async def test_retrieve_without_hash_in_claim(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """Claims missing content hash fields raise ValueError on retrieve."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payload = make_payload("no-hash-claim")
        [claim] = await driver.store(make_store_context(), [payload])

        legacy_claim = StorageDriverClaim(
            claim_data={
                "bucket": claim.claim_data["bucket"],
                "key": claim.claim_data["key"],
            },
        )
        with pytest.raises(
            ValueError,
            match=r"S3StorageDriver claim is missing required content hash information",
        ):
            await driver.retrieve(StorageDriverRetrieveContext(), [legacy_claim])


# ---------------------------------------------------------------------------
# TestS3StorageDriverBucketCallable
# ---------------------------------------------------------------------------


class TestS3StorageDriverBucketCallable:
    async def test_callable_selector_routes_bucket(
        self, aioboto3_client: S3Client, driver_client: S3StorageDriverClient
    ) -> None:
        other_bucket = "other-bucket"
        await aioboto3_client.create_bucket(Bucket=other_bucket)
        driver = S3StorageDriver(
            client=driver_client,
            bucket=lambda ctx, p: other_bucket,
        )
        [claim] = await driver.store(make_store_context(), [make_payload()])
        assert claim.claim_data["bucket"] == other_bucket

    async def test_selector_called_per_payload(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        call_count = 0

        def counting_selector(_ctx: StorageDriverStoreContext, _p: Payload) -> str:
            nonlocal call_count
            call_count += 1
            return BUCKET

        driver = S3StorageDriver(client=driver_client, bucket=counting_selector)
        await driver.store(
            make_store_context(), [make_payload(f"v{i}") for i in range(3)]
        )
        assert call_count == 3

    async def test_selector_routes_by_activity_type(
        self, aioboto3_client: S3Client, driver_client: S3StorageDriverClient
    ) -> None:
        """bucket callable can route payloads to different buckets by activity type."""
        bucket_a = "bucket-type-a"
        bucket_b = "bucket-type-b"
        await aioboto3_client.create_bucket(Bucket=bucket_a)
        await aioboto3_client.create_bucket(Bucket=bucket_b)

        type_buckets = {"type-a": bucket_a, "type-b": bucket_b}

        def type_selector(ctx: StorageDriverStoreContext, p: Payload) -> str:
            del p
            act = (
                ctx.target
                if isinstance(ctx.target, StorageDriverActivityInfo)
                else None
            )
            if act and act.type and act.type in type_buckets:
                return type_buckets[act.type]
            return BUCKET

        driver = S3StorageDriver(client=driver_client, bucket=type_selector)

        ctx_a = make_activity_context(
            namespace="ns1",
            activity_id="act1",
            activity_type="type-a",
        )
        [claim_a] = await driver.store(ctx_a, [make_payload("payload-a")])
        assert claim_a.claim_data["bucket"] == bucket_a

        ctx_b = make_activity_context(
            namespace="ns1",
            activity_id="act2",
            activity_type="type-b",
        )
        [claim_b] = await driver.store(ctx_b, [make_payload("payload-b")])
        assert claim_b.claim_data["bucket"] == bucket_b

    async def test_selector_receives_context_and_payload(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        received: list[tuple[StorageDriverStoreContext, Payload]] = []

        def capturing_selector(ctx: StorageDriverStoreContext, p: Payload) -> str:
            received.append((ctx, p))
            return BUCKET

        payload = make_payload()
        store_ctx = make_workflow_context()
        driver = S3StorageDriver(client=driver_client, bucket=capturing_selector)
        await driver.store(store_ctx, [payload])

        assert len(received) == 1
        assert received[0][0] is store_ctx
        assert received[0][1] == payload


# ---------------------------------------------------------------------------
# TestS3StorageDriverErrors
# ---------------------------------------------------------------------------


class TestS3StorageDriverErrors:
    async def test_store_nonexistent_bucket_raises(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        bucket = "does-not-exist"
        payload = make_payload()
        driver = S3StorageDriver(client=driver_client, bucket=bucket)
        expected_hash = hashlib.sha256(payload.SerializeToString()).hexdigest()
        expected_key = f"v0/d/sha256/{expected_hash}"
        with pytest.raises(RuntimeError) as exc_info:
            await driver.store(make_store_context(), [payload])
        assert (
            str(exc_info.value)
            == f"S3StorageDriver store failed [bucket={bucket}, key={expected_key}]"
        )
        assert isinstance(exc_info.value.__cause__, ClientError)
        assert (
            exc_info.value.__cause__.response.get("Error", {}).get("Code")
            == "NoSuchBucket"
        )

    async def test_retrieve_nonexistent_key_raises(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        key = "/d/sha256/nonexistent"
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        claim = StorageDriverClaim(claim_data={"bucket": BUCKET, "key": key})
        with pytest.raises(RuntimeError) as exc_info:
            await driver.retrieve(StorageDriverRetrieveContext(), [claim])
        assert (
            str(exc_info.value)
            == f"S3StorageDriver retrieve failed [bucket={BUCKET}, key={key}]"
        )
        assert isinstance(exc_info.value.__cause__, ClientError)
        assert (
            exc_info.value.__cause__.response.get("Error", {}).get("Code")
            == "NoSuchKey"
        )

    async def test_retrieve_nonexistent_bucket_raises(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        bucket = "does-not-exist"
        key = "/d/sha256/anything"
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        claim = StorageDriverClaim(claim_data={"bucket": bucket, "key": key})
        with pytest.raises(RuntimeError) as exc_info:
            await driver.retrieve(StorageDriverRetrieveContext(), [claim])
        assert (
            str(exc_info.value)
            == f"S3StorageDriver retrieve failed [bucket={bucket}, key={key}]"
        )
        assert isinstance(exc_info.value.__cause__, ClientError)
        assert (
            exc_info.value.__cause__.response.get("Error", {}).get("Code")
            == "NoSuchBucket"
        )

    async def test_bucket_callable_exception_propagates(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        selector = MagicMock(side_effect=RuntimeError("selector failed"))
        driver = S3StorageDriver(client=driver_client, bucket=selector)
        with pytest.raises(RuntimeError, match="selector failed"):
            await driver.store(make_store_context(), [make_payload()])

    def test_max_payload_size_zero_raises(self) -> None:
        with pytest.raises(
            ValueError, match="max_payload_size must be greater than zero"
        ):
            S3StorageDriver(
                client=MagicMock(spec=S3StorageDriverClient),
                bucket=BUCKET,
                max_payload_size=0,
            )

    def test_max_payload_size_negative_raises(self) -> None:
        with pytest.raises(
            ValueError, match="max_payload_size must be greater than zero"
        ):
            S3StorageDriver(
                client=MagicMock(spec=S3StorageDriverClient),
                bucket=BUCKET,
                max_payload_size=-1,
            )

    async def test_payload_exceeds_max_size_raises(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        driver = S3StorageDriver(
            client=driver_client, bucket=BUCKET, max_payload_size=10
        )
        with pytest.raises(
            ValueError,
            match=r"Payload size \d+ bytes exceeds the configured max_payload_size of 10 bytes",
        ):
            await driver.store(make_store_context(), [make_payload("exceeds-limit")])

    async def test_payload_at_max_size_succeeds(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        payload = make_payload("x")
        driver = S3StorageDriver(
            client=driver_client,
            bucket=BUCKET,
            max_payload_size=len(payload.SerializeToString()),
        )
        await driver.store(make_store_context(), [payload])


# ---------------------------------------------------------------------------
# TestS3StorageDriverConcurrency
# ---------------------------------------------------------------------------


class _AsyncBarrier:
    """Minimal asyncio.Barrier equivalent for Python <3.11."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._count = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._count += 1
        if self._count >= self._parties:
            self._event.set()
        else:
            await self._event.wait()


def _barrier_wrapper(
    fn: Callable[..., Coroutine[Any, Any, Any]], barrier: _AsyncBarrier
):
    """Wrap an async method to wait at a barrier before proceeding.

    All concurrent callers must reach the barrier before any of them continue.
    If the calls are sequential, the barrier will never be satisfied and the
    test times out.
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        return await fn(*args, **kwargs)

    return wrapper


class TestS3StorageDriverConcurrency:
    async def test_store_payloads_concurrently(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """All uploads must be in-flight concurrently.

        A barrier sized to ``num_payloads`` blocks each upload until every
        upload has started. If the driver dispatches sequentially the barrier
        is never satisfied and the test times out.
        """
        num_payloads = 5
        barrier = _AsyncBarrier(num_payloads)
        driver_client.put_object = _barrier_wrapper(driver_client.put_object, barrier)  # type: ignore[method-assign]

        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payloads = [make_payload(f"concurrent-store-{i}") for i in range(num_payloads)]

        claims = await driver.store(make_store_context(), payloads)
        assert len(claims) == num_payloads

    async def test_retrieve_payloads_concurrently(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """All downloads must be in-flight concurrently."""
        num_payloads = 5
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payloads = [
            make_payload(f"concurrent-retrieve-{i}") for i in range(num_payloads)
        ]
        claims = await driver.store(make_store_context(), payloads)

        barrier = _AsyncBarrier(num_payloads)
        driver_client.get_object = _barrier_wrapper(driver_client.get_object, barrier)  # type: ignore[method-assign]

        retrieved = await driver.retrieve(StorageDriverRetrieveContext(), claims)
        assert retrieved == payloads

    async def test_store_cancels_remaining_on_failure(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """When one upload fails, all other in-flight uploads are cancelled."""
        faulty_client = FailOnceDriverClient(
            delegate=driver_client,
            fail_on="object_exists",
        )
        driver = S3StorageDriver(client=faulty_client, bucket=BUCKET)
        payloads = [make_payload(f"cancel-store-{i}") for i in range(3)]

        with pytest.raises(
            RuntimeError,
            match=r"S3StorageDriver store failed \[bucket=.+, key=.+\]",
        ) as exc_info:
            await driver.store(make_store_context(), payloads)

        assert isinstance(exc_info.value.__cause__, ConnectionError)
        assert str(exc_info.value.__cause__) == "S3 connection lost"
        assert (
            len(faulty_client.cancelled) == 2
        ), "Expected 2 remaining tasks to be cancelled"

    async def test_retrieve_cancels_remaining_on_failure(
        self, driver_client: S3StorageDriverClient
    ) -> None:
        """When one download fails, all other in-flight downloads are cancelled."""
        driver = S3StorageDriver(client=driver_client, bucket=BUCKET)
        payloads = [make_payload(f"cancel-retrieve-{i}") for i in range(3)]
        claims = await driver.store(make_store_context(), payloads)

        faulty_client = FailOnceDriverClient(
            delegate=driver_client,
            fail_on="get_object",
        )
        driver = S3StorageDriver(client=faulty_client, bucket=BUCKET)

        with pytest.raises(
            RuntimeError,
            match=r"S3StorageDriver retrieve failed \[bucket=.+, key=.+\]",
        ) as exc_info:
            await driver.retrieve(StorageDriverRetrieveContext(), claims)

        assert isinstance(exc_info.value.__cause__, ConnectionError)
        assert str(exc_info.value.__cause__) == "S3 connection lost"
        assert (
            len(faulty_client.cancelled) == 2
        ), "Expected 2 remaining tasks to be cancelled"
