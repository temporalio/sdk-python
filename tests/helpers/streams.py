"""Stream Stores that the Stream tests share."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from temporalio.api.enums.v1 import (
    StreamErrorReason,
    StreamIdConflictPolicy,
    StreamStatus,
)
from temporalio.api.stream.v1 import (
    Producer,
    StreamRecord,
    StreamRef,
    WorkflowTaskEpoch,
)
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    CloseStreamRequest,
    CloseStreamResponse,
    CreateStreamRequest,
    CreateStreamResponse,
    DescribeStreamRequest,
    DescribeStreamResponse,
    ReadRecordsRequest,
    ReadRecordsResponse,
)
from temporalio.client import StreamStore
from temporalio.exceptions import StreamError

_RequestT = TypeVar("_RequestT", bound=Message)
_ResponseT = TypeVar("_ResponseT", bound=Message)


class RecordingStreamStore(StreamStore):
    """A Store that passes each call to another Store and keeps the operation names.

    It has the name of the Store that it wraps, since that Store writes its own name
    in the ref of a create. A test overrides :py:meth:`before_call` to fail a call or
    to hold it.
    """

    def __init__(self, inner: StreamStore) -> None:
        self.inner = inner
        self.operations: list[str] = []

    @property
    def name(self) -> str:
        return self.inner.name

    async def before_call(self, operation: str, request: Message) -> None:
        del operation, request

    async def _forward(
        self,
        operation: str,
        request: _RequestT,
        call: Callable[[_RequestT], Awaitable[_ResponseT]],
    ) -> _ResponseT:
        self.operations.append(operation)
        await self.before_call(operation, request)
        return await call(request)

    async def create_stream(self, request: CreateStreamRequest) -> CreateStreamResponse:
        return await self._forward("CreateStream", request, self.inner.create_stream)

    async def append_records(
        self, request: AppendRecordsRequest
    ) -> AppendRecordsResponse:
        return await self._forward("AppendRecords", request, self.inner.append_records)

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        return await self._forward("ReadRecords", request, self.inner.read_records)

    async def close_stream(self, request: CloseStreamRequest) -> CloseStreamResponse:
        return await self._forward("CloseStream", request, self.inner.close_stream)

    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        return await self._forward(
            "DescribeStream", request, self.inner.describe_stream
        )


@dataclasses.dataclass
class _LastBatch:
    epoch: tuple[int, ...]
    sequence: int
    response: AppendRecordsResponse


@dataclasses.dataclass
class _DictStream:
    request_id: str
    records: list[StreamRecord] = dataclasses.field(default_factory=list)
    closed: bool = False
    # The id of a Producer is unique in one Stream.
    last_batches: dict[str, _LastBatch] = dataclasses.field(default_factory=dict)
    changed: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

    def wake(self) -> None:
        """Wake the reads that wait, and give later reads a new event."""
        self.changed.set()
        self.changed = asyncio.Event()


def _offset(index: int) -> bytes:
    return index.to_bytes(8, "big")


def _epoch(producer: Producer) -> tuple[int, ...]:
    """Give a key that orders the epochs of one Producer, as ``Producer`` documents it."""
    kind = producer.WhichOneof("kind")
    task = WorkflowTaskEpoch()
    scheduled = Timestamp()
    attempt = 0
    if kind == "workflow":
        task = producer.workflow.epoch
    elif kind == "local_activity":
        task = producer.local_activity.epoch.task
        attempt = producer.local_activity.epoch.attempt
    elif kind in ("workflow_activity", "standalone_activity"):
        scheduled = getattr(producer, kind).epoch.attempt_scheduled_time
        attempt = getattr(producer, kind).epoch.attempt
    return (
        task.started_event_id,
        task.attempt,
        task.started_time.seconds,
        task.started_time.nanos,
        scheduled.seconds,
        scheduled.nanos,
        attempt,
    )


class DictStreamStore(StreamStore):
    """A Store that keeps its Streams in a dict, for the tests.

    It has no Retention and no expiry, and it checks no request. It resends the answer
    of a batch that comes again with the same Producer, epoch and sequence, and it
    fences a Producer whose epoch is lower than its newest one. A read with a ``wait``
    waits for an append or a close, as Core needs for Workflow code.
    :py:attr:`waiting` counts the reads that wait, so a test sees one that stays.
    """

    def __init__(self, name: str = "dict") -> None:
        self._name = name
        self.streams: dict[str, _DictStream] = {}
        self.waiting = 0

    @property
    def name(self) -> str:
        return self._name

    def _stream(self, ref: StreamRef) -> _DictStream:
        stream = self.streams.get(ref.id)
        if stream is None:
            raise StreamError(
                f"No Stream '{ref.id}'",
                reason=StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND,
            )
        return stream

    async def create_stream(self, request: CreateStreamRequest) -> CreateStreamResponse:
        ref = StreamRef(id=request.stream_ref.id, store=self.name)
        stream = self.streams.get(ref.id)
        if stream is None:
            self.streams[ref.id] = _DictStream(request_id=request.request_id)
            return CreateStreamResponse(ref=ref, created=True)
        if stream.request_id == request.request_id:
            return CreateStreamResponse(ref=ref, created=True)
        if (
            request.id_conflict_policy
            == StreamIdConflictPolicy.STREAM_ID_CONFLICT_POLICY_USE_EXISTING
        ):
            return CreateStreamResponse(ref=ref, created=False)
        raise StreamError(
            f"The Stream '{ref.id}' exists",
            reason=StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS,
        )

    async def append_records(
        self, request: AppendRecordsRequest
    ) -> AppendRecordsResponse:
        stream = self._stream(request.ref)
        epoch = _epoch(request.producer)
        last = stream.last_batches.get(request.producer.id)
        previous = last if last is not None and last.epoch == epoch else None
        sequence = request.sequence or (previous.sequence + 1 if previous else 1)
        if previous is not None and sequence == previous.sequence:
            return previous.response
        if stream.closed:
            raise StreamError(
                f"The Stream '{request.ref.id}' is closed",
                reason=StreamErrorReason.STREAM_ERROR_REASON_CLOSED,
            )
        if last is not None and epoch < last.epoch:
            raise StreamError(
                "The epoch is lower than the newest one of the Producer",
                reason=StreamErrorReason.STREAM_ERROR_REASON_FENCED,
            )
        first = len(stream.records) + 1
        for record in request.records:
            stream.records.append(
                StreamRecord(
                    producer=request.producer,
                    body=record.body,
                    offset=_offset(len(stream.records) + 1),
                )
            )
        response = AppendRecordsResponse(
            first_offset=_offset(first), last_offset=_offset(len(stream.records))
        )
        stream.last_batches[request.producer.id] = _LastBatch(epoch, sequence, response)
        if request.records:
            stream.wake()
        return response

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        stream = self._stream(request.ref)
        start = int.from_bytes(request.from_offset, "big") if request.from_offset else 0
        wait = request.wait.ToTimedelta().total_seconds()
        if start >= len(stream.records) and not stream.closed and wait > 0:
            self.waiting += 1
            try:
                await asyncio.wait_for(stream.changed.wait(), wait)
            except TimeoutError:
                pass
            finally:
                self.waiting -= 1
        page = stream.records[start : start + (request.max_records or 1000)]
        last_offset = page[-1].offset if page else request.from_offset
        return ReadRecordsResponse(
            records=page,
            last_offset=last_offset,
            done=stream.closed and start + len(page) == len(stream.records),
        )

    async def close_stream(self, request: CloseStreamRequest) -> CloseStreamResponse:
        stream = self._stream(request.ref)
        stream.closed = True
        stream.wake()
        return CloseStreamResponse()

    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        stream = self._stream(request.ref)
        return DescribeStreamResponse(
            status=StreamStatus.STREAM_STATUS_CLOSED
            if stream.closed
            else StreamStatus.STREAM_STATUS_OPEN,
            last_offset=stream.records[-1].offset if stream.records else b"",
        )
