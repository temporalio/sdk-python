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


class BufferedAppender(Generic[BodyT]):
    """A buffer that writes the appends of one Stream in batches.

    Use it in code that appends often, such as an Activity that streams model
    tokens. :py:meth:`append` returns immediately and does no I/O. The appender
    writes the buffered Records as one batch, at most ``max_delay`` after the
    first of them. One write runs at a time. Records appended during a write
    wait for the next batch, and they keep their order.

    Use it in ``async with``, which writes the remaining Records at the end of
    the block, or await :py:meth:`flush` before the code that appends ends.
    Otherwise the appender writes the Records that it still holds up to
    ``max_delay`` later. For an Activity that has ended, that write still has the
    Producer of its attempt, it can come after a write of the next attempt, and
    nobody gets its error.

    ``await flush()`` is the backpressure point. If code appends faster than the
    Store takes Records, the buffer grows with no bound, unless that code awaits
    ``flush()`` from time to time.

    After a batch fails, the appender writes nothing more, so a reader sees no
    gap. Each later :py:meth:`append` and :py:meth:`flush` raises the error of
    that batch. If the write of the batch was cancelled, they raise
    :exc:`RuntimeError`, and only a ``flush`` that waited for that write gets
    the cancel.

    The appender is for client and Activity code. Workflow code passes its
    Records to one ``append`` call of its Stream.

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
        """Create an appender. It starts no write before the first append.

        Args:
            stream: The Stream to append to, from a client or from the client of
                an Activity.
            max_delay: The longest time that a Record waits in the buffer before
                its batch starts to write, while no write runs. ``None`` means
                100 milliseconds.
            max_batch_records: The maximum number of Records of one batch. A
                buffer with this many Records is written immediately. ``None``
                means no maximum of the appender, and a batch over a limit of
                the client's ``StreamConfig`` then fails.

        Raises:
            RuntimeError: The appender is created in Workflow code.
            ValueError: ``max_batch_records`` is below 1.
        """
        if temporalio.workflow.in_workflow():
            raise RuntimeError(
                "BufferedAppender is for client and Activity code. In Workflow code, "
                "pass the Records to one append call."
            )
        if max_batch_records is not None and max_batch_records < 1:
            raise ValueError("max_batch_records must be 1 or more")
        self._stream = stream
        self._max_delay = (
            timedelta(milliseconds=100) if max_delay is None else max_delay
        )
        self._max_batch_records = max_batch_records
        self._buffer: list[BodyT] = []
        # When the oldest Record came into an empty buffer
        self._first_at = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._writing: asyncio.Task[None] | None = None
        self._appended = 0
        # Count of the Records that a batch wrote or that a failed batch left behind
        self._settled = 0
        self._error: Exception | None = None
        self._last_offset: bytes | None = None
        self._unwritten = 0

    def append(self, *records: BodyT) -> None:
        """Add one Record for each value to the buffer, and return immediately.

        Raises:
            temporalio.exceptions.StreamError: A batch failed, so the appender
                takes no more Records.
            RuntimeError: The write of a batch was cancelled.
        """
        if self._error:
            raise self._error
        if not self._buffer:
            self._first_at = asyncio.get_running_loop().time()
        self._buffer.extend(records)
        self._appended += len(records)
        self._schedule()

    async def flush(self) -> bytes | None:
        """Write every Record that was appended before the call.

        Returns:
            The offset of the last written Record, or ``None`` if the appender
            has written nothing.

        Raises:
            temporalio.exceptions.StreamError: A batch failed.
            RuntimeError: The write of a batch was cancelled before the call.
        """
        target = self._appended
        while self._settled < target:
            if not self._writing:
                self._start()
            assert self._writing
            # A write is one batch at the Store, so a cancel of the caller doesn't stop it.
            await asyncio.shield(self._writing)
        if self._error:
            raise self._error
        return self._last_offset

    @property
    def unwritten(self) -> int:
        """Number of Records of the failed batch and of the buffer behind it.

        The appender wrote none of the buffer. The Store may still have taken the
        failed batch, if only its answer was lost.
        """
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
        """Write the buffered Records. An error of the block wins over a write error."""
        try:
            await self.flush()
        except Exception:
            if exc is None:
                raise

    def _schedule(self) -> None:
        """Start the next batch, immediately or when the delay of its first Record ends."""
        if self._writing or not self._buffer:
            return
        if self._max_batch_records and len(self._buffer) >= self._max_batch_records:
            self._start()
        elif not self._timer:
            self._timer = asyncio.get_running_loop().call_at(
                self._first_at + self._max_delay.total_seconds(), self._start
            )

    def _start(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        batch = self._buffer[: self._max_batch_records]
        del self._buffer[: self._max_batch_records]
        self._writing = asyncio.get_running_loop().create_task(self._write(batch))

    async def _write(self, batch: list[BodyT]) -> None:
        try:
            response = await self._stream.append(*batch)
            self._last_offset = response.last_offset
        except BaseException as err:
            # The Records behind a batch that failed, or whose write was cancelled, are
            # never written, so a reader sees no gap.
            self._unwritten = len(batch) + len(self._buffer)
            self._settled += len(self._buffer)
            self._buffer.clear()
            if isinstance(err, Exception):
                self._error = err
            else:
                # Nobody cancelled a later caller, so it gets no `CancelledError`.
                self._error = RuntimeError(
                    "The write of a batch was cancelled, so the appender takes no "
                    "more Records."
                )
                raise
        finally:
            self._settled += len(batch)
            self._writing = None
            self._schedule()
