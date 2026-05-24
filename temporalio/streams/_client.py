"""StreamClient — top-level entry point for ``temporalio.streams``."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

from ._stream import Stream
from ._transport import (
    CreateStreamRequest,
    DeleteStreamRequest,
    DescribeStreamRequest,
    ListStreamsRequest,
    Transport,
)
from ._types import (
    CreateStreamOptions,
    ListStreamsPage,
    StreamDescription,
    StreamSummary,
)


class StreamClient:
    """Client for the native-streams gRPC service.

    Most callers will use :py:meth:`create_stream` or :py:meth:`stream`
    to get a :py:class:`Stream` handle and operate against that.  This
    class exists for stream-set operations (``list_streams``,
    ``delete_stream``) and for the few callers who want raw RPC access.

    Pairs with a :py:class:`Transport` that implements the wire protocol.
    For production this will be a gRPC-backed transport; for tests use
    :py:class:`FakeTransport`.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        namespace: str = "default",
    ) -> None:
        self._transport = transport
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def transport(self) -> Transport:
        return self._transport

    async def create_stream(
        self,
        stream_id: str,
        *,
        segment_max_items: int = 0,
        segment_max_bytes: int = 0,
        retention_max_bytes: int = 0,
        retention_max_items: int = 0,
        publisher_ttl: timedelta | None = None,
        owner_workflow_id: str = "",
        owner_run_id: str = "",
        created_by: str = "",
    ) -> Stream:
        """Create a new native stream.

        Per-stream TTL is intentionally not exposed (see
        ``native-streams/open-questions.html`` Q6).  Time-based
        retention is namespace policy; per-stream caps are
        ``retention_max_bytes`` / ``retention_max_items``.
        """
        opts = CreateStreamOptions(
            retention_max_bytes=retention_max_bytes,
            retention_max_items=retention_max_items,
            segment_max_items=segment_max_items,
            segment_max_bytes=segment_max_bytes,
            publisher_ttl=publisher_ttl,
            owner_workflow_id=owner_workflow_id,
            owner_run_id=owner_run_id,
            created_by=created_by,
        )
        await self._transport.create_stream(
            CreateStreamRequest(
                namespace_id=self._namespace,
                stream_id=stream_id,
                options=opts,
            )
        )
        return self.stream(stream_id)

    def stream(self, stream_id: str) -> Stream:
        """Return a :py:class:`Stream` handle bound to ``stream_id``.

        No RPC.  The handle is a thin wrapper; the first call against it
        is the first server round-trip.
        """
        return Stream(
            transport=self._transport,
            namespace=self._namespace,
            stream_id=stream_id,
        )

    async def describe_stream(self, stream_id: str) -> StreamDescription:
        return await self._transport.describe_stream(
            DescribeStreamRequest(namespace_id=self._namespace, stream_id=stream_id)
        )

    async def delete_stream(self, stream_id: str) -> None:
        await self._transport.delete_stream(
            DeleteStreamRequest(namespace_id=self._namespace, stream_id=stream_id)
        )

    async def list_streams(
        self,
        *,
        page_size: int = 100,
    ) -> AsyncIterator[StreamSummary]:
        """Iterate every stream in the namespace.

        Pages transparently using ``next_page_token``.  Yields one
        :py:class:`StreamSummary` at a time.
        """
        token: bytes | None = None
        while True:
            page: ListStreamsPage = await self._transport.list_streams(
                ListStreamsRequest(
                    namespace_id=self._namespace,
                    next_page_token=token,
                    page_size=page_size,
                )
            )
            for s in page.streams:
                yield s
            if not page.next_page_token:
                return
            token = page.next_page_token


def new_publisher_id() -> str:
    """Helper: generate a fresh ephemeral ``publisher_id``.

    Callers that want continuity across process restarts should
    persist their own ID (see
    ``native-streams/open-questions.html`` "Publisher identity
    persistence on the client" and ``exactly-once.html`` "What callers
    must not assume").
    """
    return str(uuid.uuid4())


def compute_payload_hash(items: "list[bytes]") -> bytes:
    """Compute the SHA-256 payload hash over a batch of codec-applied
    item bytes.  Server-side proof-of-write expects this hash; see
    ``native-streams/exactly-once.html`` §6 proof-of-write."""
    h = hashlib.sha256()
    for data in items:
        h.update(data)
    return h.digest()
