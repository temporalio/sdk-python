"""A helper that batches the appends of one Stream.

.. warning::
    This module is experimental and subject to change.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import TracebackType
from typing import Generic, TypeVar

from typing_extensions import Self

import temporalio.nexus.system.stream_service.client
import temporalio.workflow

BodyT = TypeVar("BodyT")

# The Store refuses an append of more Records than this. Many small Records fit under its byte
# limit, but they keep the Store busy for long.
_MAX_BATCH_RECORDS = 1000

_DEFAULT_MAX_DELAY = timedelta(milliseconds=100)


class BufferedAppender(Generic[BodyT]):
    """Batches the appends of one Stream.

    Use it in code that appends often, such as an Activity that streams model tokens.
    :py:meth:`append` returns immediately and does no I/O. One write is in flight at a time.
    Records appended during a write wait for the next batch, and they keep their order. A batch
    holds at most ``max_batch_records`` Records.

    The appender writes the buffered Records at most ``max_delay`` after the first of them, or
    immediately when they fill a batch. It is for client and Activity code. Workflow code can't
    use it, and passes its Records to one ``append`` call of the Stream.

    Use it in ``async with``, which writes the remaining Records at the end of the block, or
    await :py:meth:`flush`. Without ``async with`` or a :py:meth:`flush`, the Records still in
    the buffer are lost. The end of an Activity attempt doesn't write them.

    ``await flush()`` is the backpressure point. A caller that appends faster than the Store
    accepts Records must await it from time to time, so the buffer doesn't grow without bound.

    After a batch fails, the appender writes nothing more, so a reader sees no gap. Each later
    :py:meth:`append` and :py:meth:`flush` raises the error of that batch.

    The appender doesn't count bytes, so a batch over the size limit of the Store fails with
    the Store's error.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(
        self,
        stream: temporalio.nexus.system.stream_service.client.Stream[BodyT],
        *,
        max_delay: timedelta | None = None,
        max_batch_records: int | None = None,
    ) -> None:
        """Create an appender. It starts no write until the first append.

        Args:
            stream: The Stream to append to, from a client or from the client of an Activity.
            max_delay: The longest time that a Record waits in the buffer before its batch
                starts to write. It must not be negative. ``None`` means 100 milliseconds.
            max_batch_records: The most Records of one batch, from 1 to 1,000. Set it to the
                ``max_batch_records`` of the Store's
                :py:attr:`temporalio.client.StreamStore.limits` when the Store allows less.
                ``None`` means 1,000.

        Raises:
            RuntimeError: The appender is built in Workflow code.
            ValueError: ``max_delay`` is negative, or ``max_batch_records`` is below 1 or
                above 1,000.
        """
        if temporalio.workflow.in_workflow():
            raise RuntimeError(
                "BufferedAppender is for client and Activity code. In Workflow code, pass "
                "the Records to one append call."
            )
        if max_delay is not None and max_delay < timedelta():
            raise ValueError("max_delay must not be negative")
        if max_batch_records is None:
            max_batch_records = _MAX_BATCH_RECORDS
        elif not 1 <= max_batch_records <= _MAX_BATCH_RECORDS:
            raise ValueError(
                f"max_batch_records must be from 1 to {_MAX_BATCH_RECORDS}, "
                f"not {max_batch_records}"
            )
        self._stream = stream
        self._max_delay = max_delay
        self._max_batch_records = max_batch_records
        self._buffer: list[BodyT] = []
        # When the oldest buffered Record arrived, so its batch starts at most max_delay later.
        self._first_at = 0.0
        # The timer that starts the next batch.
        self._timer: asyncio.Handle | None = None
        self._writing: asyncio.Task[None] | None = None
        self._appended = 0
        # Count of the Records that a write took or that a failed batch left, written or not.
        self._settled = 0
        self._error: BaseException | None = None
        self._last_offset: bytes | None = None
        self._unwritten = 0

    def append(self, *records: BodyT) -> None:
        """Add one Record for each value to the buffer, and return immediately.

        A list is one value.

        Raises:
            TypeError: No value was given.
            temporalio.exceptions.StreamError: A batch failed, so the appender takes no more
                Records.
        """
        self._raise_error()
        if not records:
            raise TypeError("append takes one or more values")
        loop = asyncio.get_running_loop()
        if not self._buffer:
            self._first_at = loop.time()
        self._buffer.extend(records)
        self._appended += len(records)
        self._schedule()

    async def flush(self) -> bytes | None:
        """Write every Record appended before the call.

        Returns:
            The ``last_offset`` of the last written batch, or ``None`` if the appender has
            written nothing.

        Raises:
            temporalio.exceptions.StreamError: A batch failed.
        """
        target = self._appended
        while self._settled < target:
            if self._writing is None:
                self._start()
            assert self._writing is not None
            # The write goes on when the caller is cancelled, so no batch is left half done.
            await asyncio.shield(self._writing)
        self._raise_error()
        return self._last_offset

    @property
    def unwritten(self) -> int:
        """How many Records the failed batch and the ones after it left unwritten."""
        return self._unwritten

    async def __aenter__(self) -> Self:
        """Use the appender in ``async with``."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Write the buffered Records. A write error doesn't hide an error of the block."""
        try:
            await self.flush()
        except Exception:
            if exc is None:
                raise

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error

    def _schedule(self) -> None:
        if self._writing is not None or not self._buffer:
            return
        if len(self._buffer) >= self._max_batch_records:
            self._start()
        elif self._timer is None:
            max_delay = self._max_delay or _DEFAULT_MAX_DELAY
            self._timer = asyncio.get_running_loop().call_at(
                self._first_at + max_delay.total_seconds(), self._start
            )

    def _start(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._writing is not None or not self._buffer:
            return
        batch = self._buffer[: self._max_batch_records]
        del self._buffer[: self._max_batch_records]
        self._writing = asyncio.get_running_loop().create_task(self._write(batch))

    async def _write(self, batch: list[BodyT]) -> None:
        try:
            # The Store numbers each batch, since the appender sends no `sequence` and never
            # sends a batch twice. A `sequence` counts for a Producer on a Stream, and two
            # appenders of one Activity attempt would each start at 1.
            response = await self._stream.append(*batch)
            self._last_offset = response.last_offset
        except asyncio.CancelledError:
            # A later call must not report a cancel that its caller didn't ask for.
            self._fail(RuntimeError("The write of a batch was cancelled"), len(batch))
            raise
        except Exception as err:
            self._fail(err, len(batch))
        finally:
            self._settled += len(batch)
            self._writing = None
            # Schedule the Records appended during the write, for when their delay ends or
            # they fill a batch.
            self._schedule()

    def _fail(self, error: BaseException, count: int) -> None:
        # The Records after a failed batch are never written, so a reader sees no gap.
        self._error = error
        self._unwritten += count + len(self._buffer)
        self._settled += len(self._buffer)
        self._buffer.clear()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
