"""Stream Store that keeps its Streams in memory."""

from __future__ import annotations

import asyncio
import bisect
import collections
from dataclasses import dataclass, field
from datetime import timedelta

import google.protobuf.message

import temporalio.bridge.streams
import temporalio.runtime
from temporalio.api.enums.v1 import StreamErrorReason, StreamIdConflictPolicy
from temporalio.api.stream.v1 import (
    ActivityAttempt,
    LocalActivityAttempt,
    Producer,
    StreamRecord,
    WorkflowTaskAttempt,
)
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    CreateStreamRequest,
    CreateStreamResponse,
    DescribeStreamRequest,
    DescribeStreamResponse,
    ReadRecordsRequest,
    ReadRecordsResponse,
)
from temporalio.client._stream_store import StreamStore, _StoreCaller
from temporalio.exceptions import StreamError

_Attempt = tuple[int, ...]


class MemoryStreamStore(StreamStore):
    """Stream Store that keeps its Streams in the memory of this process.

    It's for tests and local runs. A client and a Worker see the same Streams
    only through the same instance, in one event loop.

    The Store runs the checks of SDK Core on each request and on each response,
    also for a call of client or Activity code. So a call fails on it as it fails
    on a Store package that runs those checks. The Store has no limit of a batch,
    since the limits are those of the
    :py:class:`temporalio.client.StreamConfig` of the caller.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(
        self,
        *,
        name: str = "memory",
        retention: timedelta | None = None,
        resend_window: int | None = None,
        fence: bool = False,
        runtime: temporalio.runtime.Runtime | None = None,
    ) -> None:
        """Create a memory Store.

        Args:
            name: The name of the Store in a
                :py:class:`temporalio.client.StreamConfig`.
            retention: How long after its create, or after its last append that
                wrote a batch, a Stream expires. Defaults to 7 days. A resend and
                a create of a Stream that exists don't start it again.
            resend_window: The number of recent batches of each Producer of a
                Stream whose request ids the Store remembers, so that it writes
                a resend of one of them only once. Defaults to 16, and must be
                at least 1, since a Store remembers the last batch of a Producer.
            fence: Whether the Store fences. If true, an append of an attempt
                fails with ``STREAM_ERROR_REASON_FENCED`` once a later attempt of
                its Producer has written to the Stream.
            runtime: The runtime whose threads run the checks of SDK Core.
                Defaults to :py:meth:`temporalio.runtime.Runtime.default`.
        """
        if resend_window is not None and resend_window < 1:
            raise ValueError("resend_window must be greater than zero.")
        self._name = name
        self._runtime = runtime
        streams = _MemoryStreams(
            name,
            retention=timedelta(days=7) if retention is None else retention,
            resend_window=16 if resend_window is None else resend_window,
            fence=fence,
        )
        self._caller = _StoreCaller(lambda: streams)

    def name(self) -> str:
        """See base class."""
        return self._name

    async def create_stream(
        self, request: CreateStreamRequest, *, request_id: str
    ) -> CreateStreamResponse:
        """See base class."""
        return CreateStreamResponse.FromString(
            await self._call("CreateStream", request, request_id)
        )

    async def append_records(
        self, request: AppendRecordsRequest, *, request_id: str
    ) -> AppendRecordsResponse:
        """See base class."""
        return AppendRecordsResponse.FromString(
            await self._call("AppendRecords", request, request_id)
        )

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        """See base class."""
        return ReadRecordsResponse.FromString(await self._call("ReadRecords", request))

    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        """See base class."""
        return DescribeStreamResponse.FromString(
            await self._call("DescribeStream", request)
        )

    async def _call(
        self,
        operation: str,
        request: google.protobuf.message.Message,
        request_id: str | None = None,
    ) -> bytes:
        runtime = self._runtime or temporalio.runtime.Runtime.default()
        return await temporalio.bridge.streams.call_stream_store(
            runtime._core_runtime,
            self._caller,
            operation,
            request.SerializeToString(),
            request_id,
        )


@dataclass
class _Batch:
    attempt: _Attempt
    request_id: str
    response: AppendRecordsResponse


@dataclass
class _Writer:
    """What a Stream keeps of one of its Producers."""

    window: collections.deque[_Batch]
    """The last batches that the Producer wrote, oldest first."""

    newest: _Attempt = ()
    """The latest attempt of the Producer that wrote."""


@dataclass
class _Stream:
    request_id: str
    """The request id of the create that made the Stream."""

    expires_at: float
    """The time of the event loop at which the Stream expires."""

    records: list[StreamRecord] = field(default_factory=list)
    writers: dict[bytes, _Writer] = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set at the next append that writes, which then puts a new event here."""

    @property
    def last_offset(self) -> bytes:
        return self.records[-1].offset if self.records else b""


class _MemoryStreams(StreamStore):
    """The Streams of a :py:class:`MemoryStreamStore`, which checks no request."""

    def __init__(
        self,
        name: str,
        *,
        retention: timedelta,
        resend_window: int,
        fence: bool,
    ) -> None:
        self._name = name
        self._retention = retention
        self._resend_window = resend_window
        self._fence = fence
        self._streams: dict[str, _Stream] = {}

    def name(self) -> str:
        return self._name

    async def create_stream(
        self, request: CreateStreamRequest, *, request_id: str
    ) -> CreateStreamResponse:
        stream_id = request.stream_id
        if stream_id not in self._streams:
            self._streams[stream_id] = _Stream(request_id, self._expiry())
            return CreateStreamResponse(created=True)
        stream = self._stream(stream_id)
        # An empty request id identifies no create, so it never matches.
        if request_id and request_id == stream.request_id:
            return CreateStreamResponse(created=True)
        use_existing = StreamIdConflictPolicy.STREAM_ID_CONFLICT_POLICY_USE_EXISTING
        if request.id_conflict_policy == use_existing:
            return CreateStreamResponse(created=False)
        raise StreamError(
            f"The Stream '{stream_id}' already exists",
            reason=StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS,
        )

    async def append_records(
        self, request: AppendRecordsRequest, *, request_id: str
    ) -> AppendRecordsResponse:
        stream = self._stream(request.stream_id)
        key, attempt = _producer_key(request.producer), _attempt(request.producer)
        writer = stream.writers.get(key)
        if writer is None:
            writer = _Writer(collections.deque(maxlen=self._resend_window))
        if request_id:
            for batch in writer.window:
                if (batch.attempt, batch.request_id) == (attempt, request_id):
                    return batch.response
        if self._fence and attempt < writer.newest:
            raise _fenced(request.stream_id)
        first = len(stream.records) + 1
        stream.records.extend(
            StreamRecord(
                producer=request.producer,
                body=record.body,
                offset=(first + at).to_bytes(8, "big"),
            )
            for at, record in enumerate(request.records)
        )
        response = AppendRecordsResponse(
            first_offset=first.to_bytes(8, "big"), last_offset=stream.last_offset
        )
        writer.newest = attempt
        writer.window.append(_Batch(attempt, request_id, response))
        stream.writers[key] = writer
        stream.expires_at = self._expiry()
        self._notify(stream)
        return response

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        stream = self._stream(request.stream_id)
        if request.from_offset > stream.last_offset:
            raise StreamError(
                f"The from_offset of the read is past the newest offset of the "
                f"Stream '{request.stream_id}'",
                reason=StreamErrorReason.STREAM_ERROR_REASON_OFFSET_TOO_HIGH,
            )
        start = bisect.bisect_right(
            stream.records, request.from_offset, key=lambda record: record.offset
        )
        wait = request.wait.ToTimedelta().total_seconds()
        if start == len(stream.records) and wait > 0:
            try:
                async with asyncio.timeout(wait):
                    await stream.changed.wait()
            except TimeoutError:
                pass
        end = len(stream.records)
        if request.max_records > 0:
            end = min(end, start + request.max_records)
        records = stream.records[start:end]
        return ReadRecordsResponse(
            records=records,
            last_offset=records[-1].offset if records else request.from_offset,
        )

    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        stream = self._stream(request.stream_id)
        return DescribeStreamResponse(last_offset=stream.last_offset)

    def _stream(self, stream_id: str) -> _Stream:
        stream = self._streams.get(stream_id)
        if stream is None:
            raise StreamError(
                f"The Stream '{stream_id}' doesn't exist",
                reason=StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND,
            )
        if self._now() >= stream.expires_at:
            raise StreamError(
                f"The Stream '{stream_id}' expired",
                reason=StreamErrorReason.STREAM_ERROR_REASON_EXPIRED,
            )
        return stream

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _expiry(self) -> float:
        return self._now() + self._retention.total_seconds()

    def _notify(self, stream: _Stream) -> None:
        stream.changed.set()
        stream.changed = asyncio.Event()


def _fenced(stream_id: str) -> StreamError:
    return StreamError(
        f"A later attempt of the Producer has written to the Stream '{stream_id}'",
        reason=StreamErrorReason.STREAM_ERROR_REASON_FENCED,
    )


def _kind(producer: Producer) -> google.protobuf.message.Message:
    name = producer.WhichOneof("kind")
    assert name, "SDK Core refuses a Producer with no kind"
    return getattr(producer, name)


def _producer_key(producer: Producer) -> bytes:
    """Return what identifies a Producer, which is its kind with the ids of that
    kind and without its attempt.
    """
    key = Producer()
    key.CopyFrom(producer)
    kind = _kind(key)
    if "attempt" in kind.DESCRIPTOR.fields_by_name:
        kind.ClearField("attempt")
    return key.SerializeToString(deterministic=True)


def _attempt(producer: Producer) -> _Attempt:
    """Return a value that orders the attempts of one Producer, as the attempt key
    of SDK Core does.

    Two attempts compare by the Workflow Task first (its started event id, its
    attempt number, its started time), and then by the Activity attempt (its
    scheduled time, its attempt number). A field that the kind of the Producer
    doesn't have, or that is negative, is zero. So every write of a client has
    the same attempt.
    """
    attempt = getattr(_kind(producer), "attempt", None)
    task, scheduled, number = WorkflowTaskAttempt(), ActivityAttempt(), 0
    if isinstance(attempt, WorkflowTaskAttempt):
        task = attempt
    elif isinstance(attempt, LocalActivityAttempt):
        task, number = attempt.task, attempt.attempt
    elif isinstance(attempt, ActivityAttempt):
        scheduled, number = attempt, attempt.attempt
    fields = (
        task.started_event_id,
        task.attempt,
        task.started_time.seconds,
        task.started_time.nanos,
        scheduled.attempt_scheduled_time.seconds,
        scheduled.attempt_scheduled_time.nanos,
        number,
    )
    return tuple(max(0, value) for value in fields)
