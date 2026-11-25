"""Tests of the memory Stream Store."""

import asyncio
import random
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from google.protobuf.duration_pb2 import Duration
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from temporalio.api.common.v1 import Payload
from temporalio.api.enums.v1 import (
    StreamErrorReason,
    StreamIdConflictPolicy,
)
from temporalio.api.stream.v1 import (
    ActivityAttempt,
    AppendRecord,
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
from temporalio.bridge.streams import call_stream_store
from temporalio.client._stream_store import _StoreCaller
from temporalio.exceptions import StreamError
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.testing import MemoryStreamStore
from temporalio.testing._stream_store import _MemoryStreams
from tests.helpers.streams import failure_note, run_with_virtual_clock

_NOT_FOUND = StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
_EXPIRED = StreamErrorReason.STREAM_ERROR_REASON_EXPIRED
_FENCED = StreamErrorReason.STREAM_ERROR_REASON_FENCED

# Attempts of each attempt message, earliest first. Where a later attempt wins by one field,
# its fields after that one are lower, so a comparison in another order of the fields differs.
_TASKS = [
    WorkflowTaskAttempt(started_event_id=3, attempt=2),
    WorkflowTaskAttempt(
        started_event_id=3, attempt=2, started_time=Timestamp(seconds=9)
    ),
    WorkflowTaskAttempt(
        started_event_id=3, attempt=3, started_time=Timestamp(seconds=1)
    ),
    WorkflowTaskAttempt(started_event_id=5, attempt=1),
]
_ATTEMPTS: dict[type[Message], list[Any]] = {
    WorkflowTaskAttempt: _TASKS,
    LocalActivityAttempt: [
        LocalActivityAttempt(task=_TASKS[0], attempt=2),
        LocalActivityAttempt(task=_TASKS[0], attempt=3),
        LocalActivityAttempt(task=_TASKS[1], attempt=1),
        LocalActivityAttempt(task=_TASKS[3], attempt=1),
    ],
    ActivityAttempt: [
        ActivityAttempt(
            attempt_scheduled_time=Timestamp(seconds=4, nanos=7), attempt=2
        ),
        ActivityAttempt(
            attempt_scheduled_time=Timestamp(seconds=4, nanos=7), attempt=3
        ),
        ActivityAttempt(
            attempt_scheduled_time=Timestamp(seconds=4, nanos=8), attempt=1
        ),
        ActivityAttempt(attempt_scheduled_time=Timestamp(seconds=5), attempt=1),
    ],
}


@dataclass(frozen=True)
class _Writer:
    """A generated Producer, with what a Store tells Producers and attempts apart by."""

    producer: Producer
    ids: tuple[str | int, ...]
    """The kind and each id of the Producer."""
    rank: int
    """The place of the attempt among the attempts of the Producer."""


def _writer(rng: random.Random, same: _Writer | None = None) -> _Writer:
    """Return a new Producer, or the Producer of ``same``, with an attempt."""
    producer = Producer()
    if same:
        producer.CopyFrom(same.producer)
    name = (
        str(same.ids[0])
        if same
        else rng.choice(list(Producer.DESCRIPTOR.fields_by_name))
    )
    kind = getattr(producer, name)
    kind.SetInParent()
    ids: list[str | int] = [name]
    for id_field in kind.DESCRIPTOR.fields:
        # A number id, such as the scheduled event id of an Activity, can be zero.
        values = {id_field.TYPE_STRING: "aab", id_field.TYPE_INT64: [0, 0, 5]}
        if id_field.type in values:
            if not same:
                setattr(kind, id_field.name, rng.choice(values[id_field.type]))
            ids.append(getattr(kind, id_field.name))
    rank = 0
    if attempt := kind.DESCRIPTOR.fields_by_name.get("attempt"):
        attempts = _ATTEMPTS[type(getattr(kind, attempt.name))]
        rank = rng.randrange(len(attempts))
        getattr(kind, attempt.name).CopyFrom(attempts[rank])
    return _Writer(producer, tuple(ids), rank)


@dataclass
class _ModelBatch:
    writer: _Writer
    request_id: str
    response: AppendRecordsResponse


@dataclass
class _ModelStream:
    request_id: str
    records: list[StreamRecord] = field(default_factory=list)
    batches: list[_ModelBatch] = field(default_factory=list)

    def fenced(self, writer: _Writer) -> bool:
        return any(
            batch.writer.ids == writer.ids and batch.writer.rank > writer.rank
            for batch in self.batches
        )


@dataclass
class _Model:
    """What a Store with the rules of ``StreamStore`` answers, from all that it wrote."""

    resend_window: int
    fence: bool
    streams: dict[str, _ModelStream] = field(default_factory=dict)

    def create(self, request: CreateStreamRequest, request_id: str) -> Any:
        stream = self.streams.get(request.stream_id)
        if stream is None:
            self.streams[request.stream_id] = _ModelStream(request_id)
            return CreateStreamResponse(created=True)
        if request_id and request_id == stream.request_id:
            return CreateStreamResponse(created=True)
        use_existing = StreamIdConflictPolicy.STREAM_ID_CONFLICT_POLICY_USE_EXISTING
        if request.id_conflict_policy == use_existing:
            return CreateStreamResponse()
        return StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS

    def append(
        self, request: AppendRecordsRequest, writer: _Writer, request_id: str
    ) -> Any:
        # SDK Core refuses a batch with no Record before the Store gets it.
        if not request.records:
            return StreamErrorReason.STREAM_ERROR_REASON_INVALID_ARGUMENT
        stream = self.streams.get(request.stream_id)
        if stream is None:
            return _NOT_FOUND
        own = [batch for batch in stream.batches if batch.writer.ids == writer.ids]
        for batch in own[len(own) - min(len(own), self.resend_window) :]:
            if request_id and (batch.writer.rank, batch.request_id) == (
                writer.rank,
                request_id,
            ):
                return batch.response
        if self.fence and stream.fenced(writer):
            return _FENCED
        response = AppendRecordsResponse()
        for record in request.records:
            offset = (len(stream.records) + 1).to_bytes(8, "big")
            response.first_offset = response.first_offset or offset
            response.last_offset = offset
            stream.records.append(
                StreamRecord(producer=request.producer, body=record.body, offset=offset)
            )
        stream.batches.append(_ModelBatch(writer, request_id, response))
        return response

    def read(self, request: ReadRecordsRequest) -> Any:
        stream = self.streams.get(request.stream_id)
        if stream is None:
            return _NOT_FOUND
        start = int.from_bytes(request.from_offset, "big")
        if start > len(stream.records):
            return StreamErrorReason.STREAM_ERROR_REASON_OFFSET_TOO_HIGH
        records = stream.records[start:][: request.max_records or None]
        return ReadRecordsResponse(
            records=records,
            last_offset=records[-1].offset if records else request.from_offset,
        )


_OPERATIONS = ["create", "append", "again", "read"]
_WEIGHTS = [3, 10, 6, 4]


async def _answer(call: Awaitable[Message]) -> Any:
    """Return the response of a Store call, or the reason of its refusal."""
    try:
        return await call
    except StreamError as err:
        return err.reason


async def test_the_memory_store_keeps_the_rules_of_the_redis_store() -> None:
    own = Runtime(telemetry=TelemetryConfig())
    for seed in range(150):
        with failure_note(f"seed {seed}"):
            await _store_script(random.Random(seed), None)
    # A Store with a runtime of its own runs its checks there, and not on the default one.
    with patch.object(Runtime, "default", side_effect=AssertionError("The default")):
        await _store_script(random.Random(0), own)


async def _store_script(rng: random.Random, runtime: Runtime | None) -> None:
    model = _Model(resend_window=rng.randrange(1, 4), fence=rng.random() < 0.5)
    store = MemoryStreamStore(
        resend_window=model.resend_window, fence=model.fence, runtime=runtime
    )
    appends: list[tuple[AppendRecordsRequest, _Writer, str]] = []
    writers: list[_Writer] = []
    for step in range(50):
        stream_id = rng.choice("xy")
        # Most writes come from another attempt of a Producer that the script already has.
        writer = _writer(
            rng, rng.choice(writers) if rng.random() < 0.7 * bool(writers) else None
        )
        writers.append(writer)
        request: Message
        match rng.choices(_OPERATIONS, _WEIGHTS)[0]:
            case "create":
                request = CreateStreamRequest(
                    stream_id=stream_id,
                    id_conflict_policy=rng.choice(
                        list(StreamIdConflictPolicy.values())
                    ),
                )
                request_id = rng.choice(["", "r1", "r2"])
                expected = model.create(request, request_id)
                answer = await _answer(
                    store.create_stream(request, request_id=request_id)
                )
            case "append" | "again" as op:
                request = AppendRecordsRequest(
                    stream_id=stream_id, producer=writer.producer
                )
                request_id = rng.choice(["", "r1", "r2", "r3"])
                if op == "again" and appends:
                    # A late or a repeated try has the Producer, the attempt and the
                    # request id of an earlier append, and it may have other Records.
                    earlier, writer, request_id = rng.choice(appends)
                    request.CopyFrom(earlier)
                    del request.records[:]
                for _ in range(rng.randrange(3)):
                    body = Payload(data=bytes([rng.randrange(256)]))
                    request.records.append(AppendRecord(body=body))
                appends.append((request, writer, request_id))
                expected = model.append(request, writer, request_id)
                answer = await _answer(
                    store.append_records(request, request_id=request_id)
                )
            case _:
                request = ReadRecordsRequest(
                    stream_id=stream_id,
                    from_offset=rng.choice(
                        [b"", *(i.to_bytes(8, "big") for i in range(1, 9))]
                    ),
                    max_records=rng.randrange(4),
                )
                expected = model.read(request)
                answer = await _answer(store.read_records(request))
        assert answer == expected, f"step {step}: {request}"


def _streams(retention: int = 1) -> _MemoryStreams:
    return _MemoryStreams(
        "memory", retention=timedelta(seconds=retention), resend_window=1, fence=False
    )


_S = "s"


def _client_append() -> AppendRecordsRequest:
    request = AppendRecordsRequest(stream_id=_S, records=[AppendRecord()])
    request.producer.client.SetInParent()
    return request


def test_a_stream_expires_after_its_retention_from_its_create_or_last_write() -> None:
    async def script(rng: random.Random) -> None:
        loop = asyncio.get_running_loop()
        retention = rng.randrange(1, 50)
        streams = _streams(retention)
        await streams.create_stream(CreateStreamRequest(stream_id=_S), request_id="c")
        expires_at = loop.time() + retention
        # The window of the client Producer has its last batch, so an append with the
        # request id of that batch is a resend, which writes nothing.
        last_batch: str | None = None
        for step in range(12):
            await asyncio.sleep(rng.randrange(40))
            expired = loop.time() >= expires_at
            match rng.choice(["append", "append", "read", "describe", "create"]):
                case "append":
                    request_id = rng.choice(["", "r"])
                    append = streams.append_records(
                        _client_append(), request_id=request_id
                    )
                    answer = await _answer(append)
                    expected: Any = AppendRecordsResponse
                    resend = bool(request_id) and request_id == last_batch
                    if not expired and not resend:
                        expires_at, last_batch = loop.time() + retention, request_id
                case "read":
                    read = ReadRecordsRequest(stream_id=_S)
                    answer = await _answer(streams.read_records(read))
                    expected = ReadRecordsResponse
                case "describe":
                    describe = DescribeStreamRequest(stream_id=_S)
                    answer = await _answer(streams.describe_stream(describe))
                    expected = DescribeStreamResponse
                case _:
                    use_existing = (
                        StreamIdConflictPolicy.STREAM_ID_CONFLICT_POLICY_USE_EXISTING
                    )
                    create = CreateStreamRequest(
                        stream_id=_S, id_conflict_policy=use_existing
                    )
                    answer = await _answer(
                        streams.create_stream(create, request_id=rng.choice("cd"))
                    )
                    expected = CreateStreamResponse
            if expired:
                expected = _EXPIRED
            assert answer == expected or type(answer) is expected, (
                f"step {step}: {answer} at {loop.time()}"
            )

    for seed in range(200):
        with failure_note(f"seed {seed}"):
            run_with_virtual_clock(script(random.Random(seed)))


def test_a_read_waits_for_the_next_write_or_for_its_wait() -> None:
    async def script(rng: random.Random) -> None:
        loop = asyncio.get_running_loop()
        streams = _streams(retention=100)
        await streams.create_stream(CreateStreamRequest(stream_id=_S), request_id="")
        first = await streams.append_records(_client_append(), request_id="")
        wait, write_in = rng.sample(range(1, 60), 2)
        write = rng.random() < 0.5
        read = asyncio.ensure_future(
            streams.read_records(
                ReadRecordsRequest(
                    stream_id=_S,
                    from_offset=first.last_offset,
                    wait=Duration(seconds=wait),
                )
            )
        )
        answered_at: list[float] = []
        read.add_done_callback(lambda _: answered_at.append(loop.time()))
        await asyncio.sleep(write_in)
        if write:
            await streams.append_records(_client_append(), request_id="")
        page = await read
        in_time = write and write_in < wait
        assert (
            answered_at == [float(write_in if in_time else wait)]
            and len(page.records) == in_time
            and page.last_offset
            == (page.records[-1].offset if in_time else first.last_offset)
        ), f"{page} at {answered_at}"

    for seed in range(200):
        with failure_note(f"seed {seed}"):
            run_with_virtual_clock(script(random.Random(seed)))


async def test_a_store_call_of_the_bridge_gives_the_store_the_caps_of_the_read() -> (
    None
):
    asked: list[ReadRecordsRequest] = []

    class Asked(_MemoryStreams):
        async def read_records(
            self, request: ReadRecordsRequest
        ) -> ReadRecordsResponse:
            asked.append(request)
            return await super().read_records(request)

    streams = Asked("memory", retention=timedelta(days=1), resend_window=1, fence=False)
    await streams.create_stream(CreateStreamRequest(stream_id=_S), request_id="c")
    await streams.append_records(_client_append(), request_id="a")
    await streams.append_records(_client_append(), request_id="b")
    read = ReadRecordsRequest(stream_id=_S, max_records=1, max_bytes=100)
    answer = await call_stream_store(
        Runtime.default()._core_runtime,
        _StoreCaller(lambda: streams),
        "ReadRecords",
        read.SerializeToString(),
    )
    # Only SDK Core asks for one Record more, since only it acts on whether the
    # Stream has more after the page. Client and Activity code don't.
    assert asked == [read]
    assert len(ReadRecordsResponse.FromString(answer).records) == 1


async def test_a_dropped_store_call_never_reaches_the_store() -> None:
    asked: list[str] = []

    class Waiting(MemoryStreamStore):
        async def describe_stream(
            self, request: DescribeStreamRequest
        ) -> DescribeStreamResponse:
            asked.append("call")
            try:
                return await asyncio.Future()
            finally:
                asked.append("cancel")

    def store() -> MemoryStreamStore:
        asked.append("store")
        return Waiting()

    def on_task(_task: asyncio.Task[Any]) -> None:
        raise asyncio.CancelledError

    request = DescribeStreamRequest(stream_id=_S).SerializeToString()
    with pytest.raises(asyncio.CancelledError):
        await _StoreCaller(store).call("DescribeStream", request, None, on_task)
    assert not asked

    # Core drops this call after it made the task of the call and before the task ran,
    # since the loop is blocked in between. A task that ran anyway is cancelled.
    loop = asyncio.get_running_loop()
    call = asyncio.ensure_future(
        call_stream_store(
            Runtime.default()._core_runtime,
            _StoreCaller(store),
            "DescribeStream",
            request,
        )
    )
    time.sleep(0.05)
    await asyncio.sleep(0)
    call.cancel()
    loop.call_soon(time.sleep, 0.2)
    await asyncio.sleep(0.3)
    assert asked in ([], ["store", "call", "cancel"])
