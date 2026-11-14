from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from google.protobuf.message import Message

import temporalio.api.streamservice.v1
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.client import BufferedAppender, Client
from temporalio.nexus.system.stream_service import StreamRef
from temporalio.nexus.system.stream_service.client import Stream
from temporalio.testing import MemoryStreamStore
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_store import _create_stream, _stream_error, _with_store

# Long enough that only a flush, a full batch or the end of `async with` starts a write.
_NEVER = timedelta(hours=1)


class RecordingStore(RecordingStreamStore):
    def __init__(self) -> None:
        super().__init__(MemoryStreamStore())
        self.batches: list[int] = []
        self.append_started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def before_call(self, operation: str, request: Message) -> None:
        if isinstance(request, temporalio.api.streamservice.v1.AppendRecordsRequest):
            self.batches.append(len(request.records))
            self.append_started.set()
            if self.release is not None:
                await self.release.wait()


async def _bodies(stream: Stream[str]) -> list[str]:
    return [record.body async for record in stream.iter_read_items()]


async def test_appends_inside_one_delay_go_as_one_batch(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    appender = BufferedAppender(stream, max_delay=_NEVER)
    appender.append("a")
    appender.append("b", "c")
    appender.append("d")
    await appender.flush()
    assert store.batches == [4]
    assert await _bodies(stream) == ["a", "b", "c", "d"]


async def test_flush_returns_the_last_offset(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    appender = BufferedAppender(stream)
    assert await appender.flush() is None
    appender.append("a", "b")
    offset = await appender.flush()
    assert offset is not None
    assert offset == (await stream.describe()).last_offset


async def test_appends_during_a_write_go_in_the_next_batch(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    store.release = asyncio.Event()
    appender = BufferedAppender(stream, max_delay=_NEVER)
    appender.append("a")
    first = asyncio.create_task(appender.flush())
    await store.append_started.wait()
    appender.append("b")
    appender.append("c")
    assert store.batches == [1]
    store.release.set()
    await first
    await appender.flush()
    assert store.batches == [1, 2]
    assert await _bodies(stream) == ["a", "b", "c"]


async def test_a_large_buffer_goes_as_batches_of_at_most_1000(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    appender = BufferedAppender(stream, max_delay=_NEVER)
    values = [str(index) for index in range(2500)]
    appender.append(*values)
    # A full batch doesn't wait for the delay or a flush.
    await store.append_started.wait()
    await appender.flush()
    assert store.batches == [1000, 1000, 500]
    assert await _bodies(stream) == values


async def test_the_timer_alone_writes_the_batch(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    appender = BufferedAppender(stream, max_delay=timedelta(milliseconds=10))
    appender.append("a", "b")
    page = await stream.read(wait=timedelta(seconds=10))
    assert [record.body for record in page.records] == ["a", "b"]
    assert store.batches == [2]


async def test_a_failed_batch_stops_the_appender(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    await stream.close()
    store.release = asyncio.Event()
    appender = BufferedAppender(stream, max_delay=_NEVER)
    appender.append("a")
    first = asyncio.create_task(appender.flush())
    await store.append_started.wait()
    appender.append("b", "c")
    store.release.set()
    err = await _stream_error(first)
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED
    assert await _stream_error(appender.flush()) is err
    with pytest.raises(type(err)) as later:
        appender.append("d")
    assert later.value is err
    assert appender.unwritten == 3
    assert store.batches == [1]


async def test_async_with_flushes_at_its_end(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    async with BufferedAppender(stream, max_delay=_NEVER) as appender:
        appender.append("a", "b")
    assert store.batches == [2]
    assert await _bodies(stream) == ["a", "b"]


async def test_async_with_keeps_the_error_of_its_block(client: Client):
    store = RecordingStore()
    stream = await _create_stream(client, store)
    await stream.close()
    appender = BufferedAppender(stream, max_delay=_NEVER)
    with pytest.raises(RuntimeError, match="the block failed"):
        async with appender:
            appender.append("a")
            raise RuntimeError("the block failed")
    # The flush at the end of the block ran and failed.
    assert store.batches == [1]
    assert appender.unwritten == 1
    err = await _stream_error(appender.flush())
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED


async def test_append_without_a_value_fails(client: Client):
    stream = _with_store(client, MemoryStreamStore()).get_stream(
        StreamRef(id=f"stream-{uuid.uuid4()}"), body_type=str
    )
    appender = BufferedAppender(stream)
    with pytest.raises(TypeError, match="one or more values"):
        appender.append()
    with pytest.raises(ValueError, match="max_delay must not be negative"):
        BufferedAppender(stream, max_delay=timedelta(seconds=-1))
