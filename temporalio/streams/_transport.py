"""Transport layer for ``temporalio.streams``.

The :class:`Transport` ABC is the boundary between the SDK API surface
(``StreamClient`` / ``Stream``) and the on-the-wire protocol.  Two
implementations live alongside:

- :class:`FakeTransport` — in-process records calls and returns canned
  responses; for unit tests of the SDK API.
- A future ``GrpcTransport`` (not in this milestone) will marshal these
  calls onto the chasm-lib ``StreamService`` gRPC service via the
  History service.  That implementation needs proto-binding generation
  through the Rust bridge.

Keeping the SDK pinned to this ABC means the API surface is stable
across transport implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ._types import (
    CreateStreamOptions,
    ListStreamsPage,
    PublishItem,
    PublishResult,
    StreamCloseReason,
    StreamDescription,
    StreamState,
    StreamSummary,
    TruncateResult,
)


# Internal transport-level request/response shapes.  Not part of the
# public SDK API; the client translates between user-facing types and
# these.  Aligned 1:1 with the chasm-lib StreamService proto messages.


@dataclass
class CreateStreamRequest:
    namespace_id: str
    stream_id: str
    options: CreateStreamOptions = field(default_factory=CreateStreamOptions)


@dataclass
class DescribeStreamRequest:
    namespace_id: str
    stream_id: str


@dataclass
class PublishRequest:
    namespace_id: str
    stream_id: str
    publisher_id: str
    sequence: int
    items: list[PublishItem]
    payload_hash: bytes


@dataclass
class ReadRangeRequest:
    namespace_id: str
    stream_id: str
    start_offset: int
    end_offset: int
    topics: list[str] = field(default_factory=list)


@dataclass
class ReadRangeResponse:
    items: list[PublishItem]
    # Offsets corresponding to items. Empty means the items are contiguous
    # starting at the request start_offset.
    offsets: list[int] = field(default_factory=list)
    next_offset: int = 0


@dataclass
class CloseRequest:
    namespace_id: str
    stream_id: str
    closed_by: str = ""
    close_reason: StreamCloseReason = StreamCloseReason.EXPLICIT


@dataclass
class TruncateRequest:
    namespace_id: str
    stream_id: str
    up_to_offset: int
    force: bool = False


@dataclass
class DeleteStreamRequest:
    namespace_id: str
    stream_id: str


@dataclass
class ListStreamsRequest:
    namespace_id: str
    next_page_token: bytes | None = None
    page_size: int = 0


class Transport(ABC):
    """Abstract transport for the StreamService RPCs.

    All methods raise on RPC failure.  Typed errors (``StreamClosed``,
    ``PublishAbortedByClose``, etc.) come back as
    :class:`StreamServiceError` subclasses raised by the transport.
    """

    @abstractmethod
    async def create_stream(self, req: CreateStreamRequest) -> str: ...

    @abstractmethod
    async def describe_stream(
        self, req: DescribeStreamRequest
    ) -> StreamDescription: ...

    @abstractmethod
    async def publish(self, req: PublishRequest) -> PublishResult: ...

    @abstractmethod
    async def read_range(self, req: ReadRangeRequest) -> ReadRangeResponse: ...

    @abstractmethod
    async def close_stream(self, req: CloseRequest) -> None: ...

    @abstractmethod
    async def truncate(self, req: TruncateRequest) -> TruncateResult: ...

    @abstractmethod
    async def delete_stream(self, req: DeleteStreamRequest) -> None: ...

    @abstractmethod
    async def list_streams(self, req: ListStreamsRequest) -> ListStreamsPage: ...


class StreamServiceError(Exception):
    """Base for typed errors raised by the SDK / transport."""


class StreamClosed(StreamServiceError):
    """The stream is sealed; mutators reject."""


class StreamNotFound(StreamServiceError):
    """No stream with that ID in this namespace."""


class StreamAlreadyExists(StreamServiceError):
    """create_stream called against an existing stream_id."""


class PublishAbortedByClose(StreamServiceError):
    """An in-flight publish prepared before Close cannot commit."""


class PublishAbortedByOwnerChange(StreamServiceError):
    """Shard ownership moved between Prepare and Commit; retry safely."""


class PublishGroupAborted(StreamServiceError):
    """A peer in the same group-commit failed a guard; retry safely."""


class TruncateBeyondHead(StreamServiceError):
    """truncate up_to_offset exceeds the committed head_offset."""


class TruncateBeyondBase(StreamServiceError):
    """truncate up_to_offset is below the current base_offset."""


class TruncateBlockedBySubscriber(StreamServiceError):
    """An active in-workflow subscriber pins the base; use force=True."""


class SeqRegression(StreamServiceError):
    """Sequence regression: seq < last_seq for this publisher.

    Per :py:attr:`StreamCloseReason`, the publisher must use strictly
    increasing sequence numbers within a TTL window.
    """


class OffsetTruncated(StreamServiceError):
    """Read range start_offset is below the current base_offset."""


# In-memory fake


class FakeTransport(Transport):
    """In-memory Transport for unit tests.

    Maintains a simple state machine — enough to exercise the
    ``StreamClient`` and ``Stream`` API surface without needing a real
    server.  Mirrors the chasm/lib/stream behavior at the cross-facet-
    protocol level (the cross-facet I/O is collapsed since everything is
    in one process).
    """

    @dataclass
    class _StreamState:
        stream_id: str
        head_offset: int = 0
        base_offset: int = 0
        committed_txn_id: int = 0
        closed: bool = False
        created_by: str = ""
        created_time: datetime | None = None
        closed_by: str = ""
        closed_time: datetime | None = None
        close_reason: StreamCloseReason = StreamCloseReason.UNSPECIFIED
        options: CreateStreamOptions = field(default_factory=CreateStreamOptions)
        # Per-publisher dedup state: publisher_id -> (last_seq, first_offset, item_count)
        publishers: dict[str, tuple[int, int, int]] = field(default_factory=dict)
        # Committed items, indexed by absolute offset.
        items: dict[int, PublishItem] = field(default_factory=dict)

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], FakeTransport._StreamState] = {}
        # Test-only: record every call for assertions.
        self.calls: list[tuple[str, object]] = []

    def _key(self, namespace_id: str, stream_id: str) -> tuple[str, str]:
        return (namespace_id, stream_id)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def create_stream(self, req: CreateStreamRequest) -> str:
        self.calls.append(("create_stream", req))
        key = self._key(req.namespace_id, req.stream_id)
        if key in self._streams:
            raise StreamAlreadyExists(req.stream_id)
        self._streams[key] = self._StreamState(
            stream_id=req.stream_id,
            options=req.options,
            created_by=req.options.created_by,
            created_time=self._now(),
        )
        return req.stream_id

    async def describe_stream(self, req: DescribeStreamRequest) -> StreamDescription:
        self.calls.append(("describe_stream", req))
        st = self._require(req.namespace_id, req.stream_id)
        state = StreamState(
            stream_id=st.stream_id,
            head_offset=st.head_offset,
            base_offset=st.base_offset,
            committed_txn_id=st.committed_txn_id,
            closed=st.closed,
            created_by=st.created_by,
            created_time=st.created_time,
            closed_by=st.closed_by,
            closed_time=st.closed_time,
            close_reason=st.close_reason,
            owner_workflow_id=st.options.owner_workflow_id,
            owner_run_id=st.options.owner_run_id,
            retention_max_bytes=st.options.retention_max_bytes,
            retention_max_items=st.options.retention_max_items,
            segment_max_items=st.options.segment_max_items,
            segment_max_bytes=st.options.segment_max_bytes,
        )
        return StreamDescription(
            state=state,
            inflight_count=0,
            subscription_count=0,
            publisher_count=len(st.publishers),
        )

    async def publish(self, req: PublishRequest) -> PublishResult:
        self.calls.append(("publish", req))
        st = self._require(req.namespace_id, req.stream_id)
        if st.closed:
            raise StreamClosed(req.stream_id)
        # Dedup-replay / monotonic-seq guard.
        pub = st.publishers.get(req.publisher_id)
        if pub is not None:
            last_seq, prior_first, prior_count = pub
            if req.sequence < last_seq:
                raise SeqRegression(f"seq {req.sequence} < last_seq {last_seq}")
            if req.sequence == last_seq:
                return PublishResult(first_offset=prior_first, item_count=prior_count)
        first_offset = st.head_offset
        item_count = len(req.items)
        st.committed_txn_id += 1
        for i, item in enumerate(req.items):
            st.items[first_offset + i] = item
        st.head_offset += item_count
        st.publishers[req.publisher_id] = (
            req.sequence,
            first_offset,
            item_count,
        )
        return PublishResult(first_offset=first_offset, item_count=item_count)

    async def read_range(self, req: ReadRangeRequest) -> ReadRangeResponse:
        self.calls.append(("read_range", req))
        st = self._require(req.namespace_id, req.stream_id)
        if req.start_offset < st.base_offset:
            raise OffsetTruncated(req.stream_id)
        items: list[PublishItem] = []
        offsets: list[int] = []
        scan_end = min(req.end_offset, st.head_offset)
        for off in range(req.start_offset, scan_end):
            it = st.items.get(off)
            if it is None:
                break
            if req.topics and it.topic not in req.topics:
                continue
            items.append(it)
            offsets.append(off)
        return ReadRangeResponse(items=items, offsets=offsets, next_offset=scan_end)

    async def close_stream(self, req: CloseRequest) -> None:
        self.calls.append(("close_stream", req))
        st = self._require(req.namespace_id, req.stream_id)
        if st.closed:
            raise StreamClosed(req.stream_id)
        st.closed = True
        st.closed_by = req.closed_by
        st.close_reason = req.close_reason
        st.closed_time = self._now()

    async def truncate(self, req: TruncateRequest) -> TruncateResult:
        self.calls.append(("truncate", req))
        st = self._require(req.namespace_id, req.stream_id)
        if st.closed:
            raise StreamClosed(req.stream_id)
        if req.up_to_offset > st.head_offset:
            raise TruncateBeyondHead(req.stream_id)
        if req.up_to_offset < st.base_offset:
            raise TruncateBeyondBase(req.stream_id)
        # Subscriber-pin guard skipped — FakeTransport has no subscriber
        # support yet.
        for off in list(st.items.keys()):
            if off < req.up_to_offset:
                del st.items[off]
        st.base_offset = req.up_to_offset
        return TruncateResult(
            new_base_offset=st.base_offset,
            force_truncated_subscription_count=0,
        )

    async def delete_stream(self, req: DeleteStreamRequest) -> None:
        self.calls.append(("delete_stream", req))
        key = self._key(req.namespace_id, req.stream_id)
        if key not in self._streams:
            raise StreamNotFound(req.stream_id)
        del self._streams[key]

    async def list_streams(self, req: ListStreamsRequest) -> ListStreamsPage:
        self.calls.append(("list_streams", req))
        streams: list[StreamSummary] = []
        for (ns, sid), st in self._streams.items():
            if ns != req.namespace_id:
                continue
            streams.append(
                StreamSummary(
                    stream_id=sid,
                    head_offset=st.head_offset,
                    base_offset=st.base_offset,
                    closed=st.closed,
                    created_time=st.created_time,
                    closed_time=st.closed_time,
                )
            )
        streams.sort(key=lambda s: s.stream_id)
        start = int(req.next_page_token.decode("ascii")) if req.next_page_token else 0
        if req.page_size <= 0:
            return ListStreamsPage(streams=streams[start:])
        end = start + req.page_size
        next_page_token = str(end).encode("ascii") if end < len(streams) else None
        return ListStreamsPage(
            streams=streams[start:end], next_page_token=next_page_token
        )

    def _require(self, namespace_id: str, stream_id: str) -> _StreamState:
        st = self._streams.get(self._key(namespace_id, stream_id))
        if st is None:
            raise StreamNotFound(stream_id)
        return st
