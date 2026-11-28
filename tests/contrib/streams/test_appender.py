"""Tests of the buffered appender, with no server."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.contrib.streams import BufferedAppender
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import AppendRecordsResponse
from tests.helpers.streams import run_with_virtual_clock


class _FakeStream:
    """Stream whose append takes time on the clock of the loop, and whose batch
    number ``fails_at`` fails, or has its write cancelled.
    """

    def __init__(
        self, write_time: float, fails_at: int | None = None, cancelled: bool = False
    ) -> None:
        self._write_time = write_time
        self._fails_at = fails_at
        self._cancelled = cancelled
        self.written: list[int] = []
        self.batches: list[tuple[float, float, tuple[int, ...]]] = []
        """The start time, the end time and the Records of each batch."""

    async def append(self, *records: int) -> AppendRecordsResponse:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.sleep(self._write_time)
            if len(self.batches) == self._fails_at:
                if self._cancelled:
                    raise asyncio.CancelledError()
                raise StreamError(
                    "The Store is down",
                    reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE,
                )
            self.written.extend(records)
            offset = len(self.written).to_bytes(8, "big")
            return AppendRecordsResponse(first_offset=offset, last_offset=offset)
        finally:
            self.batches.append((started, loop.time(), records))


@pytest.mark.parametrize("delay,seconds", [(None, 0.1), (timedelta(0), 0.0)])
def test_the_batch_delay_starts_with_the_first_record(
    delay: timedelta | None, seconds: float
) -> None:
    async def script() -> None:
        stream: Any = _FakeStream(0)
        appender = BufferedAppender[int](stream, max_delay=delay)
        appender.append(1)
        if seconds:
            await asyncio.sleep(seconds / 2)
        appender.append(2)
        await asyncio.sleep(1)
        assert stream.batches == [(seconds, seconds, (1, 2))]

    run_with_virtual_clock(script())


def test_full_batches_wait_for_the_previous_write() -> None:
    async def script() -> None:
        stream: Any = _FakeStream(2)
        appender = BufferedAppender[int](
            stream, max_delay=timedelta(seconds=3), max_batch_records=2
        )
        assert await appender.flush() is None
        appender.append(1, 2)
        await asyncio.sleep(1)
        appender.append(3, 4, 5)
        await asyncio.sleep(6)
        assert stream.batches == [(0, 2, (1, 2)), (2, 4, (3, 4)), (4, 6, (5,))]
        assert await appender.flush() == (5).to_bytes(8, "big")

    run_with_virtual_clock(script())


def test_cancelling_a_flush_preserves_the_write_and_buffer() -> None:
    async def script() -> None:
        stream: Any = _FakeStream(2)
        async with BufferedAppender[int](
            stream, max_delay=timedelta(seconds=90)
        ) as appender:
            appender.append(1)
            flushing = asyncio.create_task(appender.flush())
            await asyncio.sleep(1)
            appender.append(2)
            flushing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await flushing
        assert stream.batches == [(0, 2, (1,)), (2, 4, (2,))]

    run_with_virtual_clock(script())


@pytest.mark.parametrize("cancelled", [False, True])
async def test_a_failed_batch_stops_later_writes(cancelled: bool) -> None:
    stream: Any = _FakeStream(0, fails_at=1, cancelled=cancelled)
    appender = BufferedAppender[int](stream, max_batch_records=2)
    appender.append(1, 2, 3, 4, 5)
    with pytest.raises(asyncio.CancelledError if cancelled else StreamError):
        await appender.flush()
    assert stream.written == [1, 2]
    assert appender.unwritten == 3
    later = RuntimeError if cancelled else StreamError
    with pytest.raises(later) as raised:
        appender.append(6)
    with pytest.raises(later) as again:
        await appender.flush()
    assert raised.value is again.value


async def test_append_defers_immediate_writes_with_an_eager_task_factory() -> None:
    eager = getattr(asyncio, "eager_task_factory", None)
    if eager is None:
        pytest.skip("Eager task factories require Python 3.12")
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    stream = Mock()
    stream.append = AsyncMock(return_value=AppendRecordsResponse(last_offset=b"last"))
    try:
        loop.set_task_factory(eager)
        appender = BufferedAppender[int](stream, max_batch_records=1)
        appender.append(1)
        appender.append(2)
        stream.append.assert_not_awaited()
        assert await appender.flush() == b"last"
        assert stream.append.await_args_list == [call(1), call(2)]
    finally:
        loop.set_task_factory(previous)


@pytest.mark.parametrize("cancelled", [False, True])
def test_async_with_keeps_the_error_of_its_block(cancelled: bool) -> None:
    async def block() -> None:
        stream: Any = _FakeStream(0.0, fails_at=0, cancelled=cancelled)
        async with BufferedAppender[int](stream) as appender:
            appender.append(1)
            raise KeyError("The block failed")

    with pytest.raises(KeyError):
        run_with_virtual_clock(block())
