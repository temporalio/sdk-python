"""Tests of the buffered appender, with no server."""

import asyncio
import random
from datetime import timedelta
from typing import Any

import pytest

from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.contrib.streams import BufferedAppender
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import AppendRecordsResponse
from tests.helpers.streams import failure_note, run_with_virtual_clock


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
        self.writes = 0
        self.overlaps = 0

    async def append(self, *records: int) -> AppendRecordsResponse:
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.overlaps += self.writes
        self.writes += 1
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
            self.writes -= 1
            self.batches.append((started, loop.time(), records))


def test_buffered_appender_writes_in_order_one_batch_at_a_time() -> None:
    for seed in range(500):
        with failure_note(f"seed {seed}"):
            run_with_virtual_clock(_appends_script(random.Random(seed)))


async def _appends_script(rng: random.Random) -> None:
    loop = asyncio.get_running_loop()
    max_delay = rng.choice([None, 0.0, 0.02, 0.5])
    max_batch_records = rng.choice([None, None, 1, 2, 5])
    fails_at = rng.choice([None, None, rng.randrange(6)])
    write_time = rng.choice([0.0, 0.03, 0.3])
    cancelled = rng.random() < 0.3
    stream: Any = _FakeStream(write_time, fails_at, cancelled=cancelled)
    appender = BufferedAppender[int](
        stream,
        max_delay=None if max_delay is None else timedelta(seconds=max_delay),
        max_batch_records=max_batch_records,
    )
    delay = 0.1 if max_delay is None else max_delay
    # Sleeps that end inside a delay, inside a write, and after both
    sleeps = [0, delay / 3, delay, write_time / 3, write_time, delay + write_time + 1]
    # The time that each appended Record came at, by the Record
    arrived: list[float] = []
    # The time from the start of each flush to its end
    flushes: list[tuple[float, float]] = []
    # The count of ended batches at the start of the flush that the block waits in
    flushing: int | None = None
    error: BaseException | None = None
    try:
        async with appender:
            for _ in range(rng.randrange(1, 15)):
                action = rng.choice(["append", "sleep"] * 3 + ["flush", "cancel"])
                called_at, appended = loop.time(), len(arrived)
                if action == "append":
                    count = rng.randrange(1, 4)
                    appender.append(*range(appended, appended + count))
                    arrived += [called_at] * count
                elif action == "sleep":
                    await asyncio.sleep(rng.choice(sleeps))
                elif action == "flush":
                    flushes.append((called_at, float("inf")))
                    flushing = len(stream.batches)
                    offset = await appender.flush()
                    flushing = None
                    flushes[-1] = (called_at, loop.time())
                    # A flush writes each Record that came before it.
                    assert stream.written[:appended] == list(range(appended))
                    assert int.from_bytes(offset or b"", "big") == len(stream.written)
                else:
                    # A cancelled flush doesn't stop the write that it waits for.
                    flush = asyncio.ensure_future(appender.flush())
                    await asyncio.sleep(rng.choice(sleeps[:4]))
                    flush.cancel()
                    flushes.append((called_at, loop.time()))
            if rng.random() < 0.5:
                # The timer alone writes what is left, with no flush.
                await asyncio.sleep(1000)
                assert stream.written == list(range(len(arrived))) or appender.unwritten
            flushes.append((loop.time(), float("inf")))
            flushing = len(stream.batches)
    except (StreamError, RuntimeError, asyncio.CancelledError) as err:
        error = err

    # The written Records are the first of the appended ones, in their order.
    assert stream.written == list(range(len(stream.written)))
    assert stream.overlaps == 0
    assert len(stream.written) + appender.unwritten == len(arrived)
    failed = fails_at is not None and len(stream.batches) > fails_at
    assert (error is not None) == failed == bool(appender.unwritten)
    if error:
        # Nothing is written after the batch that failed, and each later call raises its
        # error. After a cancelled write that is a `RuntimeError`, as nobody cancelled it.
        assert len(stream.batches) == (fails_at or 0) + 1
        later = RuntimeError if cancelled else StreamError
        with pytest.raises(later) as raised:
            appender.append(0)
        with pytest.raises(later) as again:
            await appender.flush()
        assert raised.value is again.value
        # Only a flush that waited for the cancelled write got its cancel.
        waited = flushing is not None and flushing <= (fails_at or 0)
        if cancelled and waited:
            assert isinstance(error, asyncio.CancelledError)
        else:
            assert error is raised.value
    previous_end = float("-inf")
    for started, ended, records in stream.batches:
        assert 0 < len(records) <= (max_batch_records or len(records))
        due = arrived[records[0]] + delay
        # A batch starts once the delay of its first Record ends, or when the write
        # before it ends, if that is later.
        assert started <= max(due, previous_end) + 1e-9
        # Only a flush starts a batch with no maximum earlier.
        flushed = any(start <= started <= end for start, end in flushes)
        assert max_batch_records or flushed or started >= due - 1e-9
        previous_end = ended


def test_async_with_keeps_the_error_of_its_block() -> None:
    async def block() -> None:
        stream: Any = _FakeStream(0.0, fails_at=0)
        async with BufferedAppender[int](stream) as appender:
            appender.append(1)
            raise KeyError("The block failed")

    with pytest.raises(KeyError):
        run_with_virtual_clock(block())
