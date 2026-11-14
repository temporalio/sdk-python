"""A Workflow read of a Stream that waits for a predicate, and a fold over its Records.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Generic, TypeVar

from ._context import _Runtime

if TYPE_CHECKING:
    # The generated Stream modules import `temporalio.workflow`, so this module names
    # their types for the type checker only.
    from temporalio.nexus.system.stream_service.models import StreamRecord
    from temporalio.nexus.system.stream_service.workflow._resources.stream import (
        Stream,
    )

__all__ = [
    "DEFAULT_READ_STREAM_MAX_RECORDS",
    "StreamRead",
    "StreamReadStop",
    "StreamReduce",
    "read_stream",
    "reduce_stream",
]

BodyT = TypeVar("BodyT")
ValueT = TypeVar("ValueT")

DEFAULT_READ_STREAM_MAX_RECORDS = 1_000
"""The most Records of one read or fold when ``max_records`` is ``None``."""


class StreamReadStop(enum.Enum):
    """Why a read or a fold of a Stream ended.

    When several hold, the first in this order is the reason. ``MATCH``, ``DONE``,
    ``MAX_RECORDS``, ``MAX_BYTES``, ``IDLE``, ``TIMEOUT``.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    MATCH = "MATCH"
    """``until`` was true for the last Record."""

    DONE = "DONE"
    """The Stream is closed and its last Record was read."""

    MAX_RECORDS = "MAX_RECORDS"
    """The read got ``max_records`` Records."""

    MAX_BYTES = "MAX_BYTES"
    """The next Record would pass ``max_bytes``. The next read gets it."""

    IDLE = "IDLE"
    """No new Record arrived for ``idle_timeout``."""

    TIMEOUT = "TIMEOUT"
    """``timeout`` passed."""


@dataclass(frozen=True)
class StreamRead(Generic[BodyT]):
    """What one :py:func:`read_stream` gave.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    records: Sequence[StreamRecord[BodyT]]
    """The Records of the read in order, typed as a client's read gives them."""

    last_offset: bytes | None
    """The offset of the last Record, or the ``from_offset`` of a read with none.

    Give it as the ``from_offset`` of the next read.
    """

    stop: StreamReadStop
    """Why the read ended."""

    done: bool
    """Whether the Stream is closed and its last Record is in :py:attr:`records`.

    It is the state of the Stream, so it can be true when :py:attr:`stop` is ``MATCH``.
    """


@dataclass(frozen=True)
class StreamReduce(Generic[ValueT]):
    """What one :py:func:`reduce_stream` gave.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    value: ValueT
    """The value after the last Record, or ``initial`` when the fold got none."""

    count: int
    """The number of Records that the fold got."""

    last_offset: bytes | None
    """The offset of the last Record, or the ``from_offset`` of a fold with none.

    Give it as the ``from_offset`` of the next read or fold.
    """

    stop: StreamReadStop
    """Why the fold ended."""

    done: bool
    """Whether the Stream is closed and the fold got its last Record."""


@dataclass(frozen=True)
class _StreamReadLimits:
    """Where a read starts and what stops it, after the checks of the public functions."""

    from_offset: bytes | None
    idle_timeout: timedelta | None
    timeout: timedelta
    max_records: int
    max_bytes: int | None

    @staticmethod
    def check(
        *,
        from_offset: bytes | None,
        idle_timeout: timedelta | None,
        timeout: timedelta,
        max_records: int | None,
        max_bytes: int | None,
    ) -> _StreamReadLimits:
        if timeout <= timedelta():
            raise ValueError("timeout must be positive")
        if idle_timeout is not None and idle_timeout < timedelta():
            raise ValueError("idle_timeout must not be negative")
        if max_records is None:
            max_records = DEFAULT_READ_STREAM_MAX_RECORDS
        elif max_records < 1:
            raise ValueError("max_records must be at least 1")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        return _StreamReadLimits(
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            timeout=timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )


async def read_stream(
    stream: Stream[BodyT],
    *,
    until: Callable[[StreamRecord[BodyT]], bool] | None = None,
    from_offset: bytes | None = None,
    idle_timeout: timedelta | None = None,
    timeout: timedelta,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> StreamRead[BodyT]:
    """Read a Stream until a predicate is true, in one Local Activity.

    The read starts after ``from_offset`` and ends at the first stop of
    :py:class:`StreamReadStop`. A Store's Local Activity reads the pages, and History
    gets one marker for the whole read. The marker has the range of offsets and no
    Record. A replay reads that range from the Store again before the Workflow sees the
    result, so the Records must still be in the Store.

    Args:
        stream: The Workflow ``Stream`` to read, from
            :py:func:`temporalio.workflow.get_stream` or
            :py:func:`temporalio.workflow.create_stream`.
        until: Called with each typed Record in order. The read stops after the first
            Record for which it is true, and Records after it stay for the next read.
            It is called once for each Record, when the Record arrives. A later change
            of the state that it reads does not check an earlier Record again. It runs
            in the Worker process, on the Worker's event loop, while the Local Activity
            reads. It runs outside the Workflow's own thread. So it must not change any
            state, and it can't call a function of ``temporalio.workflow``. It can read
            the Workflow's state through a reference such as ``self``. When it sees a
            change of that state is not defined. A replay does not call it, since the
            replay takes the end of the read from History. ``None`` reads until another
            stop.
        from_offset: Read after this offset. ``None`` is the beginning.
        idle_timeout: End the read when no new Record arrived for this long. It counts
            from the last Record that the read got, or from the start of the read when
            it got none. ``timedelta(0)`` gives what is in the Stream now and waits for
            nothing. ``None`` is no idle stop.
        timeout: The most time for the read. It must be positive.
        max_records: The most Records to read. ``None`` is
            :py:data:`DEFAULT_READ_STREAM_MAX_RECORDS`.
        max_bytes: A cap on the bytes of the Records, as the Store counts them for a
            page of the client ``read``. A Record that would pass the cap ends the read
            before it, and the next read gets it. The first Record is always returned.

    Returns:
        The Records of the read, the offset to read after next, and why it ended.

    Raises:
        temporalio.exceptions.StreamError: The Store failed the read.
        temporalio.exceptions.ApplicationError: ``until`` raised. The error has the
            message of the exception.
        temporalio.exceptions.StreamConfigError: The Worker's client has no Stream
            config. This fails the Workflow Task.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    limits = _StreamReadLimits.check(
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        timeout=timeout,
        max_records=max_records,
        max_bytes=max_bytes,
    )
    return await _Runtime.current().workflow_read_stream(stream, until, limits)


async def reduce_stream(
    stream: Stream[BodyT],
    function: Callable[[ValueT, StreamRecord[BodyT]], ValueT],
    initial: ValueT,
    *,
    until: Callable[[ValueT, StreamRecord[BodyT]], bool] | None = None,
    value_type: type[ValueT] | None = None,
    from_offset: bytes | None = None,
    idle_timeout: timedelta | None = None,
    timeout: timedelta,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> StreamReduce[ValueT]:
    """Fold the Records of a Stream into one value, in one Local Activity.

    The Local Activity calls ``value = function(value, record)`` for each Record in
    order, then ``until(value, record)``. The fold ends at the first stop of
    :py:class:`StreamReadStop`, with the same rules as :py:func:`read_stream`. No
    Record comes back to the Workflow. History keeps the value in the marker of the
    Local Activity, so a replay takes the value from History and needs no Store. The
    size of the value is the caller's choice, and History pays for it.

    Args:
        stream: The Workflow ``Stream`` to fold.
        function: Gives the next value from the value so far and a typed Record. It
            changes no state other than the value that it returns, so it must not
            change ``initial`` or a value in place. It runs in the Worker process, on
            the Worker's event loop, outside the Workflow's own thread. It does no I/O,
            and it can't call a function of ``temporalio.workflow``. A replay does not
            call it.
        initial: The value before the first Record.
        until: Called with the value after each Record and that Record. The fold stops
            after the first Record for which it is true. It is called once for each
            Record, when the Record arrives. It has the rules of ``function``.
        value_type: The type that the Workflow decodes the value as. ``None`` takes the
            type of ``initial``.
        from_offset: Fold the Records after this offset. ``None`` is the beginning.
        idle_timeout: As for :py:func:`read_stream`.
        timeout: As for :py:func:`read_stream`.
        max_records: As for :py:func:`read_stream`.
        max_bytes: As for :py:func:`read_stream`.

    Returns:
        The value, the number of Records, the offset to read after next, and why the
        fold ended.

    Raises:
        temporalio.exceptions.StreamError: The Store failed the read.
        temporalio.exceptions.ApplicationError: ``function`` or ``until`` raised. The
            error has the message of the exception.
        temporalio.exceptions.StreamConfigError: The Worker's client has no Stream
            config. This fails the Workflow Task.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    limits = _StreamReadLimits.check(
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        timeout=timeout,
        max_records=max_records,
        max_bytes=max_bytes,
    )
    return await _Runtime.current().workflow_reduce_stream(
        stream,
        function,
        initial,
        until,
        value_type if value_type is not None else type(initial),
        limits,
    )
