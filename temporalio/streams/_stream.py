"""Stream — convenience handle bound to a single ``(namespace, stream_id)``."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from ._transport import Transport
from ._transport import (
    CloseRequest,
    DescribeStreamRequest,
    PublishRequest,
    ReadRangeRequest,
    Transport,
    TruncateRequest,
)
from ._types import (
    PublishItem,
    PublishResult,
    StreamCloseReason,
    StreamDescription,
    TruncateResult,
)


class Stream:
    """Handle bound to a single ``(namespace, stream_id)``.

    Construct via :py:meth:`StreamClient.create_stream` or
    :py:meth:`StreamClient.stream`.  Most operations are thin wrappers
    around the :py:class:`Transport` methods; the :py:meth:`publisher`
    helper does the client-side coalescing the design assumes.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        namespace: str,
        stream_id: str,
    ) -> None:
        self._transport = transport
        self._namespace = namespace
        self._stream_id = stream_id

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def namespace(self) -> str:
        return self._namespace

    async def describe(self) -> StreamDescription:
        return await self._transport.describe_stream(
            DescribeStreamRequest(
                namespace_id=self._namespace, stream_id=self._stream_id
            )
        )

    async def publish(
        self,
        items: list[PublishItem] | list[bytes],
        *,
        publisher_id: str,
        sequence: int,
    ) -> PublishResult:
        """Append a batch under a ``(publisher_id, sequence)`` dedup key.

        Server enforces exactly-once on retry — see
        ``native-streams/exactly-once.html`` "Contract".  Sequence
        numbers must be strictly increasing per ``publisher_id``; a
        regression raises :py:class:`SeqRegression`.

        Items can be raw ``bytes`` or :py:class:`PublishItem` objects.
        Bytes are wrapped into :py:class:`PublishItem` with empty topic.
        """
        publish_items = _normalize_items(items)
        payload_hash = _payload_hash(publish_items)
        return await self._transport.publish(
            PublishRequest(
                namespace_id=self._namespace,
                stream_id=self._stream_id,
                publisher_id=publisher_id,
                sequence=sequence,
                items=publish_items,
                payload_hash=payload_hash,
            )
        )

    def publisher(
        self,
        *,
        publisher_id: str,
        start_sequence: int = 1,
        max_batch_items: int = 64,
        max_batch_bytes: int = 1 << 20,
        flush_interval_ms: int = 10,
    ) -> Publisher:
        """Return a client-side batching :py:class:`Publisher`.

        The publisher coalesces back-to-back ``publish(item)`` calls
        into one server RPC up to the batch caps (or until
        ``flush_interval_ms`` elapses since the first buffered item).
        Matches the SDK-side coalescing the design assumes — see
        ``native-streams/design-overview.html`` §1a "SDK coalesces
        back-to-back items".
        """
        return Publisher(
            stream=self,
            publisher_id=publisher_id,
            start_sequence=start_sequence,
            max_batch_items=max_batch_items,
            max_batch_bytes=max_batch_bytes,
            flush_interval_ms=flush_interval_ms,
        )

    async def read_range(
        self,
        *,
        start_offset: int = 0,
        end_offset: int | None = None,
        topics: list[str] | None = None,
        page_size: int = 1000,
    ) -> AsyncIterator[tuple[int, PublishItem]]:
        """Iterate items in ``[start_offset, end_offset)`` in order.

        Yields ``(offset, item)`` pairs.  If ``end_offset`` is ``None``,
        reads up to the current head and stops; supply a much-larger
        value to follow the stream by polling.

        Pages transparently using the response ``next_offset``.
        """
        cursor = start_offset
        # Cap unbounded reads at a sane value.
        upper = end_offset if end_offset is not None else 1 << 62
        while cursor < upper:
            window_end = min(cursor + page_size, upper)
            resp = await self._transport.read_range(
                ReadRangeRequest(
                    namespace_id=self._namespace,
                    stream_id=self._stream_id,
                    start_offset=cursor,
                    end_offset=window_end,
                    topics=list(topics) if topics else [],
                )
            )
            if resp.offsets and len(resp.offsets) != len(resp.items):
                raise RuntimeError("ReadRangeResponse offsets/items length mismatch")
            offsets = resp.offsets or list(range(cursor, cursor + len(resp.items)))
            for offset, item in zip(offsets, resp.items):
                yield offset, item

            next_offset = resp.next_offset or cursor + len(resp.items)
            if next_offset <= cursor:
                return
            cursor = next_offset

    async def close(
        self,
        *,
        closed_by: str = "",
        close_reason: StreamCloseReason = StreamCloseReason.EXPLICIT,
    ) -> None:
        await self._transport.close_stream(
            CloseRequest(
                namespace_id=self._namespace,
                stream_id=self._stream_id,
                closed_by=closed_by,
                close_reason=close_reason,
            )
        )

    async def truncate(
        self,
        up_to_offset: int,
        *,
        force: bool = False,
    ) -> TruncateResult:
        return await self._transport.truncate(
            TruncateRequest(
                namespace_id=self._namespace,
                stream_id=self._stream_id,
                up_to_offset=up_to_offset,
                force=force,
            )
        )


class Publisher:
    """Client-side batching publisher.

    Use as an async context manager — the ``__aexit__`` flushes any
    buffered items so a clean exit doesn't leave a partial batch.
    Concurrent publishes from one Publisher are not supported; use
    distinct Publisher instances (or distinct publisher_ids) for
    parallelism.
    """

    def __init__(
        self,
        *,
        stream: Stream,
        publisher_id: str,
        start_sequence: int = 1,
        max_batch_items: int = 64,
        max_batch_bytes: int = 1 << 20,
        flush_interval_ms: int = 10,
    ) -> None:
        self._stream = stream
        self._publisher_id = publisher_id
        self._next_sequence = start_sequence
        self._max_batch_items = max_batch_items
        self._max_batch_bytes = max_batch_bytes
        self._flush_interval = flush_interval_ms / 1000.0
        self._buffer: list[PublishItem] = []
        self._buffer_bytes = 0
        self._flush_lock = asyncio.Lock()
        self._flusher: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Cancel the in-flight scheduled flush (if any) so we don't
        # race the explicit flush.
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush()

    @property
    def publisher_id(self) -> str:
        return self._publisher_id

    async def publish(self, item: PublishItem | bytes) -> None:
        """Buffer one item; flushes opportunistically.

        Does NOT return per-item offsets — use :py:meth:`flush` or
        :py:meth:`Stream.publish` for that.  This API is for fire-and-
        forget streaming where the caller doesn't track individual
        offsets.
        """
        pi = _to_publish_item(item)
        self._buffer.append(pi)
        self._buffer_bytes += len(pi.data)
        if (
            len(self._buffer) >= self._max_batch_items
            or self._buffer_bytes >= self._max_batch_bytes
        ):
            await self.flush()
            return
        self._ensure_flusher()

    async def flush(self) -> PublishResult | None:
        async with self._flush_lock:
            if not self._buffer:
                return None
            batch = list(self._buffer)
            batch_bytes = self._buffer_bytes
            seq = self._next_sequence
            result = await self._stream.publish(
                batch,
                publisher_id=self._publisher_id,
                sequence=seq,
            )
            # Advance the dedup sequence only after the server ack. If the
            # call fails, the buffered bytes remain in place for retry.
            del self._buffer[: len(batch)]
            self._buffer_bytes -= batch_bytes
            self._next_sequence = seq + 1
            self._ensure_flusher()
            return result

    async def _scheduled_flush(self) -> None:
        try:
            await asyncio.sleep(self._flush_interval)
        except asyncio.CancelledError:
            return
        try:
            await self.flush()
        except Exception:
            # The explicit flush path will surface the error and retry the
            # still-buffered batch.
            pass
        else:
            self._ensure_flusher(replace_current=True)

    def _ensure_flusher(self, *, replace_current: bool = False) -> None:
        if not self._buffer:
            return
        if replace_current or self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._scheduled_flush())


def _normalize_items(items: list[PublishItem] | list[bytes]) -> list[PublishItem]:
    return [_to_publish_item(it) for it in items]


def _to_publish_item(item: PublishItem | bytes) -> PublishItem:
    if isinstance(item, PublishItem):
        return item
    return PublishItem(data=item)


def _payload_hash(items: list[PublishItem]) -> bytes:
    h = hashlib.sha256()
    for it in items:
        h.update(it.data)
    return h.digest()
