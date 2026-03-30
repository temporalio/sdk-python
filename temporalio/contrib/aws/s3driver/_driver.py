"""Amazon S3 storage driver for Temporal external storage.

.. warning::
    This API is experimental.
"""

from __future__ import annotations

import asyncio
import hashlib
import urllib.parse
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, TypeVar

from temporalio.api.common.v1 import Payload
from temporalio.contrib.aws.s3driver._client import S3StorageDriverClient
from temporalio.converter import (
    ActivitySerializationContext,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    WorkflowSerializationContext,
)

_T = TypeVar("_T")


async def _gather_with_cancellation(
    coros: Sequence[Coroutine[Any, Any, _T]],
) -> list[_T]:
    """Run coroutines concurrently, cancelling all remaining tasks if one fails."""
    if not coros:
        return []
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class S3StorageDriver(StorageDriver):
    """Driver for storing and retrieving Temporal payloads in Amazon S3.

    Requires an :class:`S3StorageDriverClient` and a ``bucket``. Payloads are keyed by
    a SHA-256 hash of their serialized bytes, segmented by namespace and
    workflow/activity identifiers derived from the serialization context.

    .. warning::
           This API is experimental.
    """

    def __init__(
        self,
        client: S3StorageDriverClient,
        bucket: str | Callable[[StorageDriverStoreContext, Payload], str],
        driver_name: str = "aws.s3driver",
        max_payload_size: int = 50 * 1024 * 1024,
    ):
        """Constructs the S3 driver.

        Args:
            client: An :class:`S3StorageDriverClient` implementation. Use
                :func:`~temporalio.contrib.aws.s3driver.aioboto3.new_aioboto3_client` to
                wrap an aioboto3 S3 client.
            bucket: S3 bucket name, access point ARN, or a callable that
                accepts ``(StorageDriverStoreContext, Payload)`` and returns
                a bucket name. A callable allows dynamic per-payload bucket
                selection.
            driver_name: Name of this driver instance. Defaults to
                ``"aws.s3driver"``. Override when registering
                multiple S3StorageDriver instances with distinct configurations
                under the same :attr:`~temporalio.extstore.Options.drivers` list.
            max_payload_size: Maximum serialized payload size in bytes that the
                driver will accept. Defaults to 52428800 (50 MiB). Raise this
                value if your workload requires larger payloads; lower it to
                enforce stricter limits.
        """
        if max_payload_size <= 0:
            raise ValueError("max_payload_size must be greater than zero")
        self._client = client
        self._bucket = bucket
        self._driver_name = driver_name or "aws.s3driver"
        self._max_payload_size = max_payload_size

    def name(self) -> str:
        """Return the driver instance name."""
        return self._driver_name

    def type(self) -> str:
        """Return the driver type identifier."""
        return "aws.s3driver"

    def _get_bucket(self, context: StorageDriverStoreContext, payload: Payload) -> str:
        """Resolve bucket using the configured strategy."""
        if callable(self._bucket):
            return self._bucket(context, payload)
        return self._bucket

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        """Stores payloads in S3 and returns a :class:`~temporalio.extstore.DriverClaim` for each one.

        Payloads are keyed by their SHA-256 hash, so identical serialized bytes
        share the same S3 object. Deduplication is best-effort because the same
        Python value may serialize differently across payload converter versions
        (e.g. proto binary). The returned list is the same length as
        ``payloads``.
        """
        workflow_id: str | None = None
        activity_id: str | None = None
        namespace: str | None = None
        if isinstance(context.serialization_context, WorkflowSerializationContext):
            workflow_id = context.serialization_context.workflow_id
            namespace = context.serialization_context.namespace
        if isinstance(context.serialization_context, ActivitySerializationContext):
            # Prioritize workflow over activity so that the same payload that
            # may be stored across workflow and activity boundaries are deduplicated.
            if context.serialization_context.workflow_id:
                workflow_id = context.serialization_context.workflow_id
            elif context.serialization_context.activity_id:
                activity_id = context.serialization_context.activity_id
            namespace = context.serialization_context.namespace

        # URL encode values to avoid characters that break the key format
        # e.g. spaces, forward-slashes, etc.
        if namespace:
            namespace = urllib.parse.quote(namespace, safe="")
        if workflow_id:
            workflow_id = urllib.parse.quote(workflow_id, safe="")
        if activity_id:
            activity_id = urllib.parse.quote(activity_id, safe="")

        namespace_segments = f"/ns/{namespace}" if namespace else ""

        context_segments = ""
        # Prioritize workflow over activity so that the same payload that
        # may be stored across workflow and activity boundaries are deduplicated.
        # Workflow and Activity IDs are case sensitive.
        if workflow_id:
            context_segments += f"/wfi/{workflow_id}"
        elif activity_id:
            context_segments += f"/aci/{activity_id}"

        async def _upload(payload: Payload) -> StorageDriverClaim:
            bucket = self._get_bucket(context, payload)

            payload_bytes = payload.SerializeToString()
            if len(payload_bytes) > self._max_payload_size:
                raise ValueError(
                    f"Payload size {len(payload_bytes)} bytes exceeds the configured "
                    f"max_payload_size of {self._max_payload_size} bytes"
                )

            hash_digest = hashlib.sha256(payload_bytes).hexdigest().lower()

            digest_segments = f"/d/sha256/{hash_digest}"

            key = f"v0{namespace_segments}{context_segments}{digest_segments}"

            try:
                if not await self._client.object_exists(bucket=bucket, key=key):
                    await self._client.put_object(
                        bucket=bucket, key=key, data=payload_bytes
                    )
            except Exception as e:
                raise RuntimeError(
                    f"S3StorageDriver store failed [bucket={bucket}, key={key}]"
                ) from e

            return StorageDriverClaim(
                claim_data={
                    "bucket": bucket,
                    "key": key,
                    "hash_algorithm": "sha256",
                    "hash_value": hash_digest,
                },
            )

        return await _gather_with_cancellation([_upload(p) for p in payloads])

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,  # noqa: ARG002
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        """Retrieves payloads from S3 for the given :class:`~temporalio.extstore.DriverClaim` list."""

        async def _download(claim: StorageDriverClaim) -> Payload:
            bucket = claim.claim_data["bucket"]
            key = claim.claim_data["key"]

            try:
                payload_bytes = await self._client.get_object(bucket=bucket, key=key)
            except Exception as e:
                raise RuntimeError(
                    f"S3StorageDriver retrieve failed [bucket={bucket}, key={key}]"
                ) from e

            expected_hash = claim.claim_data.get("hash_value")
            hash_algorithm = claim.claim_data.get("hash_algorithm")
            if expected_hash and hash_algorithm:
                if hash_algorithm != "sha256":
                    raise ValueError(
                        f"S3StorageDriver unsupported hash algorithm "
                        f"[bucket={bucket}, key={key}]: "
                        f"expected sha256, got {hash_algorithm}"
                    )
                actual_hash = hashlib.sha256(payload_bytes).hexdigest().lower()
                if actual_hash != expected_hash:
                    raise ValueError(
                        f"S3StorageDriver integrity check failed "
                        f"[bucket={bucket}, key={key}]: "
                        f"expected {hash_algorithm}:{expected_hash}, "
                        f"got {hash_algorithm}:{actual_hash}"
                    )

            payload = Payload()
            payload.ParseFromString(payload_bytes)
            return payload

        return await _gather_with_cancellation([_download(c) for c in claims])
