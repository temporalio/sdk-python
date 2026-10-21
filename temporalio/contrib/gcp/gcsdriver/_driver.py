"""Google Cloud Storage driver for Temporal external storage.

.. warning::
    This API is experimental.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, TypeVar

from temporalio.api.common.v1 import Payload
from temporalio.contrib.gcp.gcsdriver._client import GCSStorageDriverClient
from temporalio.converter import (
    StorageDriver,
    StorageDriverActivityInfo,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)

_T = TypeVar("_T")

_GCS_UNSAFE_CHARS = frozenset('#[]*?:"<>|/%')


def _percent_encode(val: str | None) -> str | None:
    """Percent-encode a segment of a GCS object name.

    Escape characters that interfere with GCS tooling, path separators, and
    percent signs, preserving readable Unicode. Returns ``None`` for empty or
    missing values so callers can substitute ``"null"``.
    """
    if not val:
        return None
    encoded: list[str] = []
    for char in val:
        if char in _GCS_UNSAFE_CHARS or ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F:
            encoded.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            encoded.append(char)
    segment = "".join(encoded)
    if segment in (".", ".."):
        return segment.replace(".", "%2E")
    return segment


def _format_client_context(client: GCSStorageDriverClient) -> str:
    """Format the client's ``describe()`` output as ", k=v, k=v" for error
    messages. Returns an empty string when the client reports no metadata or
    describe itself raises (diagnostic output must never mask the real error).
    """
    try:
        info = client.describe()
    except Exception:
        return ""
    if not info:
        return ""
    return "".join(f", {k}={v}" for k, v in info.items())


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


class GCSStorageDriver(StorageDriver):
    """Driver for storing and retrieving Temporal payloads in Google Cloud Storage.

    Requires an :class:`GCSStorageDriverClient` and a ``bucket``. Payloads are keyed by
    a SHA-256 hash of their serialized bytes, segmented by namespace and
    workflow/activity identifiers derived from the serialization context.

    .. warning::
           This API is experimental.
    """

    def __init__(
        self,
        client: GCSStorageDriverClient,
        bucket: str | Callable[[StorageDriverStoreContext, Payload], str],
        driver_name: str = "gcp.gcsdriver",
        max_payload_size: int = 50 * 1024 * 1024,
    ):
        """Constructs the GCS driver.

        Args:
            client: An :class:`GCSStorageDriverClient` implementation. Use
                :func:`temporalio.contrib.gcp.gcsdriver.gcloud_aio.new_gcloud_aio_client` to
                wrap a gcloud-aio-storage client.
            bucket: GCS bucket name or a callable that
                accepts ``(StorageDriverStoreContext, Payload)`` and returns
                a bucket name. A callable allows dynamic per-payload bucket
                selection.
            driver_name: Name of this driver instance. Defaults to
                ``"gcp.gcsdriver"``. Override when registering
                multiple GCSStorageDriver instances with distinct configurations
                under the same ``temporalio.converter.ExternalStorage.drivers`` list.
            max_payload_size: Maximum serialized payload size in bytes that the
                driver will accept. Defaults to 52428800 (50 MiB). Raise this
                value if your workload requires larger payloads; lower it to
                enforce stricter limits.
        """
        if max_payload_size <= 0:
            raise ValueError("max_payload_size must be greater than zero")
        self._client = client
        self._bucket = bucket
        self._driver_name = driver_name or "gcp.gcsdriver"
        self._max_payload_size = max_payload_size

    def name(self) -> str:
        """Return the driver instance name."""
        return self._driver_name

    def type(self) -> str:
        """Return the driver type identifier."""
        return "gcp.gcsdriver"

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
        """Stores payloads in GCS and returns a ``StorageDriverClaim`` for each one.

        Payloads are keyed by their SHA-256 hash, so identical serialized bytes
        share the same GCS object. Deduplication is best-effort because the same
        Python value may serialize differently across payload converter versions
        (e.g. proto binary). The returned list is the same length as
        ``payloads``.
        """
        context_segments = ""
        target = context.target
        namespace = _percent_encode(target.namespace) if target is not None else None
        namespace_segment = f"/ns/{namespace or 'null'}" if target is not None else ""
        if isinstance(target, StorageDriverWorkflowInfo):
            wf_type = _percent_encode(target.type) or "null"
            wf_id = _percent_encode(target.id) or "null"
            wf_run_id = _percent_encode(target.run_id) or "null"
            context_segments = f"/wt/{wf_type}/wi/{wf_id}/ri/{wf_run_id}"
        elif isinstance(target, StorageDriverActivityInfo):
            act_type = _percent_encode(target.type) or "null"
            act_id = _percent_encode(target.id) or "null"
            act_run_id = _percent_encode(target.run_id) or "null"
            context_segments = f"/at/{act_type}/ai/{act_id}/ri/{act_run_id}"

        async def _upload(payload: Payload) -> StorageDriverClaim:
            bucket = self._get_bucket(context, payload)

            payload_bytes = payload.SerializeToString()
            if len(payload_bytes) > self._max_payload_size:
                raise ValueError(
                    f"Payload size {len(payload_bytes)} bytes exceeds the configured "
                    f"max_payload_size of {self._max_payload_size} bytes"
                )

            hash_digest = hashlib.sha256(payload_bytes).hexdigest()

            digest_segments = f"/d/sha256/{hash_digest}"

            object_name = f"v0{namespace_segment}{context_segments}{digest_segments}"
            if len(object_name.encode("utf-8")) > 1024:
                # Retain namespace scope for authorization policies on object prefixes.
                object_name = f"v0{namespace_segment}{digest_segments}"

            try:
                if not await self._client.object_exists(
                    bucket=bucket, object_name=object_name
                ):
                    await self._client.put_object(
                        bucket=bucket, object_name=object_name, data=payload_bytes
                    )
            except Exception as e:
                raise RuntimeError(
                    f"GCSStorageDriver store failed [bucket={bucket}, object_name={object_name}"
                    f"{_format_client_context(self._client)}]"
                ) from e

            return StorageDriverClaim(
                claim_data={
                    "bucket": bucket,
                    "object_name": object_name,
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
        """Retrieves payloads from GCS for the given ``StorageDriverClaim`` list."""

        async def _download(claim: StorageDriverClaim) -> Payload:
            bucket = claim.claim_data["bucket"]
            object_name = claim.claim_data["object_name"]

            try:
                payload_bytes = await self._client.get_object(
                    bucket=bucket, object_name=object_name
                )
            except Exception as e:
                raise RuntimeError(
                    f"GCSStorageDriver retrieve failed [bucket={bucket}, object_name={object_name}"
                    f"{_format_client_context(self._client)}]"
                ) from e

            hash_algorithm = claim.claim_data.get("hash_algorithm")
            expected_hash = claim.claim_data.get("hash_value")
            if not hash_algorithm or not expected_hash:
                raise ValueError(
                    f"GCSStorageDriver claim is missing required content hash information "
                    f"[bucket={bucket}, object_name={object_name}]: "
                    f"claim_data must contain 'hash_algorithm' and 'hash_value'"
                )
            if hash_algorithm != "sha256":
                raise ValueError(
                    f"GCSStorageDriver unsupported hash algorithm "
                    f"[bucket={bucket}, object_name={object_name}]: "
                    f"expected sha256, got {hash_algorithm}"
                )
            actual_hash = hashlib.sha256(payload_bytes).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError(
                    f"GCSStorageDriver integrity check failed "
                    f"[bucket={bucket}, object_name={object_name}]: "
                    f"expected {hash_algorithm}:{expected_hash}, "
                    f"got {hash_algorithm}:{actual_hash}"
                )

            payload = Payload()
            payload.ParseFromString(payload_bytes)
            return payload

        return await _gather_with_cancellation([_download(c) for c in claims])
