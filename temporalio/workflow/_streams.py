"""Calls that scan a Stream until a stop: take, filter and reduce.

Client, Activity and Workflow code call the same three functions. A generated method of the
client ``Stream`` and of the Workflow ``Stream`` calls them too.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import typing
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._context import _Runtime

if TYPE_CHECKING:
    # The generated Stream modules import `temporalio.workflow`, so this module names
    # their types for the type checker only.
    import temporalio.nexus.system.stream_service.client
    import temporalio.nexus.system.stream_service.workflow._resources.stream
    from temporalio.nexus.system.stream_service.models import StreamRecord

__all__ = [
    "DEFAULT_STREAM_IDLE_TIMEOUT",
    "DEFAULT_STREAM_MAX_BYTES",
    "DEFAULT_STREAM_MAX_RECORDS",
    "MAX_STREAM_RESULT_BYTES",
    "StreamRead",
    "StreamReadStop",
    "StreamReduce",
    "filter_stream",
    "reduce_stream",
    "take_stream",
]

BodyT = TypeVar("BodyT")
ValueT = TypeVar("ValueT")

DEFAULT_STREAM_IDLE_TIMEOUT = timedelta(seconds=1)
"""How long a call waits for a new Record when ``idle_timeout`` is ``None``."""

DEFAULT_STREAM_MAX_RECORDS = 1_000
"""The most Records of one call when ``max_records`` is ``None``."""

DEFAULT_STREAM_MAX_BYTES = 256 * 1024
"""The most bytes of the Records of one call when ``max_bytes`` is ``None``."""

MAX_STREAM_RESULT_BYTES = 1024 * 1024
"""The most bytes of the encoded result of one call.

In Workflow code the result goes into History, so this bounds what one call adds to it. A
call over it raises :py:class:`temporalio.exceptions.ApplicationError`. Client and Activity
code have the same limit, so a call gives the same result everywhere.
"""


class StreamReadStop(enum.IntEnum):
    """Why a call on a Stream ended.

    When several hold, the first in the order of the values is the reason.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    Match = 0
    """``until`` accepted a Record."""

    Done = 1
    """The Stream is closed and the call looked at its last Record."""

    MaxRecords = 2
    """The call got ``max_records`` Records."""

    MaxBytes = 3
    """The next Record would pass ``max_bytes``. The next call gets it."""

    Idle = 4
    """No new Record arrived for ``idle_timeout``."""

    Timeout = 5
    """``timeout`` passed."""


@dataclasses.dataclass(slots=True, kw_only=True)
class StreamRead(Generic[BodyT]):
    """What :py:func:`take_stream` or :py:func:`filter_stream` gave.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    records: list[StreamRecord[BodyT]]
    """The Records that the call gives back, in order."""

    last_offset: bytes | None = None
    """Where the scan stopped.

    It is the offset of the last Record that the scan looked at, kept or not, or the
    ``from_offset`` of a call that looked at none. Give it as the ``from_offset`` of the next
    call, so a loop loses and repeats no Record.
    """

    stop: StreamReadStop
    """Why the call ended."""

    done: bool
    """Whether the Stream is closed and the scan looked at its last Record.

    It is the state of the Stream, so it can be true when :py:attr:`stop` is ``Match``.
    """


@dataclasses.dataclass(slots=True, kw_only=True)
class StreamReduce(Generic[ValueT]):
    """What :py:func:`reduce_stream` gave.

    .. warning::
        This API is an experimental prototype and subject to change.
    """

    value: ValueT
    """The value after the last Record, or ``initial`` when the fold got none."""

    count: int
    """The number of Records that the fold got."""

    last_offset: bytes | None = None
    """Where the scan stopped, as in :py:attr:`StreamRead.last_offset`."""

    stop: StreamReadStop
    """Why the fold ended."""

    done: bool
    """Whether the Stream is closed and the fold got its last Record."""


class _StreamCallKind(enum.Enum):
    TAKE = "take"
    FILTER = "filter"
    REDUCE = "reduce"


@dataclasses.dataclass(frozen=True)
class _StreamCall:
    """One call after the checks of the public functions. It holds the caller's functions."""

    kind: _StreamCallKind
    from_offset: bytes | None
    timeout: timedelta
    idle_timeout: timedelta
    max_records: int
    max_bytes: int
    until: Callable[..., object] | None = None
    predicate: Callable[..., object] | None = None
    function: Callable[..., object] | None = None
    initial: Any = None
    value_type: Any = None
    """The type that Workflow code decodes the value of a fold as, or ``None`` for no hint."""

    @staticmethod
    def check(
        kind: _StreamCallKind,
        *,
        timeout: timedelta,
        from_offset: bytes | None,
        idle_timeout: timedelta | None,
        max_records: int | None,
        max_bytes: int | None,
        until: Callable[..., object] | None = None,
        predicate: Callable[..., object] | None = None,
        function: Callable[..., object] | None = None,
        initial: Any = None,
        value_type: Any = None,
    ) -> _StreamCall:
        if not isinstance(timeout, timedelta) or timeout <= timedelta():
            raise ValueError("timeout must be a positive timedelta")
        if idle_timeout is None:
            idle_timeout = DEFAULT_STREAM_IDLE_TIMEOUT
        elif idle_timeout < timedelta():
            raise ValueError("idle_timeout must not be negative")
        if max_records is None:
            max_records = DEFAULT_STREAM_MAX_RECORDS
        elif max_records < 1:
            raise ValueError("max_records must be at least 1")
        if max_bytes is None:
            max_bytes = DEFAULT_STREAM_MAX_BYTES
        elif max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        for label, value, required in (
            ("until", until, False),
            ("predicate", predicate, kind is _StreamCallKind.FILTER),
            ("function", function, kind is _StreamCallKind.REDUCE),
        ):
            _check_plain_function(label, value, required=required)
        return _StreamCall(
            kind=kind,
            from_offset=from_offset,
            timeout=timeout,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
            until=until,
            predicate=predicate,
            function=function,
            initial=initial,
            value_type=value_type,
        )


def _check_plain_function(label: str, value: object, *, required: bool) -> None:
    if value is None:
        if required:
            raise TypeError(f"{label} is required")
        return
    if not callable(value):
        raise TypeError(f"{label} must be a function")
    # The scan calls the function and uses its answer at once, so it can't await one.
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(
        getattr(value, "__call__", None)
    ):
        raise TypeError(
            f"{label} must be a plain function, not a coroutine function. "
            "The scan calls it for each Record and can't await it."
        )


def _fold_value_type(
    function: Callable[..., object], initial: Any, value_type: Any
) -> Any:
    if value_type is not None:
        return value_type
    try:
        hints = typing.get_type_hints(function)
    except Exception:
        hints = {}
    if "return" in hints:
        return hints["return"]
    return type(initial) if initial is not None else None


async def _run_stream_call(stream: Any, call: _StreamCall) -> Any:
    runtime = _Runtime.maybe_current()
    if runtime is not None:
        return await runtime.workflow_stream_call(stream, call)
    # The client package imports this module, so its scan is imported at the first call.
    from temporalio.client._stream_read import _stream_call_outside_workflow

    return await _stream_call_outside_workflow(stream, call)


async def take_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[BodyT],
    timeout: timedelta,
    until: Callable[[StreamRecord[BodyT]], bool] | None = None,
    from_offset: bytes | None = None,
    idle_timeout: timedelta | None = None,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> StreamRead[BodyT]:
    """Give every Record of a Stream up to a stop.

    ``until(record)`` is called for each Record, and the first Record that it accepts is the
    last one given back.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop of :py:class:`StreamReadStop` that holds, in the order of their
    values. ``last_offset`` in the result is where the scan stopped. Give it as the
    ``from_offset`` of the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its result are the same in each. In a
    client or an Activity, the call reads the Store straight away.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the Records and their bodies. Records that the scan
    looked at and didn't keep stay out of History. The Worker's data converter, with its
    payload codec, encodes the marker. A replay takes the result from History, so it calls
    no Store and needs no Stream config. A replay doesn't call the functions either, so an
    appender in them writes nothing. A Workflow Task that fails and runs again calls them
    again, and an appender writes again with a higher epoch. The functions run on the
    Worker's event loop, not in the Workflow's thread. They can read Workflow state through
    a reference such as ``self``, but can't call ``temporalio.workflow``. Each sees a Record
    once, so a later change of that state doesn't check an earlier Record again.

    Args:
        stream: The Stream to scan.
        timeout: The most time for the call. It must be positive.
        until: Ends the call after the first Record that it accepts. ``None`` scans
            until another stop.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow code, a longer
            wait keeps the Workflow Task open, so a Signal, an Update or a cancel waits for
            the next Workflow Task heartbeat, and each heartbeat adds events to History.
        max_records: The most Records to give. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to give. A Record that would pass
            it ends the call before that Record, and the next call gets it. The first
            Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_BYTES`, 256 KiB.

    Returns:
        The Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.ApplicationError: A function raised, or returned an
            awaitable. Or the encoded result is over :py:data:`MAX_STREAM_RESULT_BYTES`,
            and the message names both sizes. In Workflow code it is also raised when the
            Worker's client has no Stream config, with the ``type`` ``StreamConfigError``.
            These are not retryable, so an uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.TAKE,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
    )
    return await _run_stream_call(stream, call)


async def filter_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[BodyT],
    predicate: Callable[[StreamRecord[BodyT]], bool],
    timeout: timedelta,
    until: Callable[[StreamRecord[BodyT]], bool] | None = None,
    from_offset: bytes | None = None,
    idle_timeout: timedelta | None = None,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> StreamRead[BodyT]:
    """Give the Records of a Stream that a predicate keeps, up to a stop.

    ``predicate(record)`` decides whether a Record is given back. ``until(record)`` is
    called for every Record that the scan looks at, kept or not. ``max_records`` and
    ``max_bytes`` count the kept Records. ``max_records=1`` gives the first Record that
    ``predicate`` keeps.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop of :py:class:`StreamReadStop` that holds, in the order of their
    values. ``last_offset`` in the result is where the scan stopped. Give it as the
    ``from_offset`` of the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its result are the same in each. In a
    client or an Activity, the call reads the Store straight away.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the kept Records and their bodies. Records that the
    scan looked at and didn't keep stay out of History. The Worker's data converter, with
    its payload codec, encodes the marker. A replay takes the result from History, so it
    calls no Store and needs no Stream config. A replay doesn't call the functions either,
    so an appender in them writes nothing. A Workflow Task that fails and runs again calls
    them again, and an appender writes again with a higher epoch. The functions run on the
    Worker's event loop, not in the Workflow's thread. They can read Workflow state through
    a reference such as ``self``, but can't call ``temporalio.workflow``. Each sees a Record
    once, so a later change of that state doesn't check an earlier Record again.

    Args:
        stream: The Stream to scan.
        predicate: Keeps a Record when it returns true.
        timeout: The most time for the call. It must be positive.
        until: Ends the call after the first Record that it accepts, kept or not.
            ``None`` scans until another stop.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow code, a longer
            wait keeps the Workflow Task open, so a Signal, an Update or a cancel waits for
            the next Workflow Task heartbeat, and each heartbeat adds events to History.
        max_records: The most Records to keep. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to keep. A Record that would pass
            it ends the call before that Record, and the next call gets it. The first
            Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_BYTES`, 256 KiB.

    Returns:
        The kept Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.ApplicationError: A function raised, or returned an
            awaitable. Or the encoded result is over :py:data:`MAX_STREAM_RESULT_BYTES`,
            and the message names both sizes. In Workflow code it is also raised when the
            Worker's client has no Stream config, with the ``type`` ``StreamConfigError``.
            These are not retryable, so an uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.FILTER,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
        predicate=predicate,
    )
    return await _run_stream_call(stream, call)


async def reduce_stream(
    stream: temporalio.nexus.system.stream_service.client.Stream[BodyT]
    | temporalio.nexus.system.stream_service.workflow._resources.stream.Stream[BodyT],
    function: Callable[[ValueT, StreamRecord[BodyT]], ValueT],
    initial: ValueT,
    timeout: timedelta,
    until: Callable[[ValueT, StreamRecord[BodyT]], bool] | None = None,
    from_offset: bytes | None = None,
    idle_timeout: timedelta | None = None,
    max_records: int | None = None,
    max_bytes: int | None = None,
    *,
    value_type: type[ValueT] | None = None,
) -> StreamReduce[ValueT]:
    """Fold the Records of a Stream into one value, up to a stop.

    The scan calls ``value = function(value, record)`` for each Record in order, then
    ``until(value, record)``. No Record comes back, only the value. ``function`` must not
    change ``initial`` or a value in place.

    The scan starts after ``from_offset`` and reads pages with the Store's long poll. It ends
    at the first stop of :py:class:`StreamReadStop` that holds, in the order of their
    values. ``last_offset`` in the result is where the scan stopped. Give it as the
    ``from_offset`` of the next call, so a loop loses and repeats no Record.

    Client, Activity and Workflow code call it with a client ``Stream`` or a Workflow
    ``Stream``. The scan, its stops, its defaults and its result are the same in each. In a
    client or an Activity, the call reads the Store straight away.

    The functions of the caller must be plain functions that change no state. The scan calls
    each one for a Record and uses its answer at once. A
    :py:class:`temporalio.contrib.streams.BufferedAppender` can append in them, and the call
    writes the appender's Records before it returns. A failed write fails the call with the
    :py:class:`temporalio.exceptions.StreamError`.

    In Workflow code only, the scan runs in one Local Activity and History gets one marker.
    The marker holds the result, with the value. Records that the scan looked at and didn't
    keep stay out of History. The Worker's data converter, with its payload codec, encodes
    the marker. A replay takes the result from History, so it calls no Store and needs no
    Stream config. A replay doesn't call the functions either, so an appender in them writes
    nothing. A Workflow Task that fails and runs again calls them again, and an appender
    writes again with a higher epoch. The functions run on the Worker's event loop, not in
    the Workflow's thread. They can read Workflow state through a reference such as
    ``self``, but can't call ``temporalio.workflow``. Each sees a Record once, so a later
    change of that state doesn't check an earlier Record again.

    Args:
        stream: The Stream to fold.
        function: Gives the next value from the value so far and a Record.
        initial: The value before the first Record.
        timeout: The most time for the call. It must be positive.
        until: Called with the value after each Record and that Record. Ends the
            fold after the first Record that it accepts.
        from_offset: Scan the Records after this offset. ``None`` is the beginning.
        idle_timeout: End the call with ``Idle`` when no new Record arrived for this long.
            It counts from the last Record that the scan got, or from the start of the
            call. ``timedelta(0)`` gives what is in the Stream now. ``None`` is
            :py:data:`DEFAULT_STREAM_IDLE_TIMEOUT`, 1 second. In Workflow code, a longer
            wait keeps the Workflow Task open, so a Signal, an Update or a cancel waits for
            the next Workflow Task heartbeat, and each heartbeat adds events to History.
        max_records: The most Records to fold. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_RECORDS`, 1,000.
        max_bytes: A cap on the bytes of the Records to fold. A Record that would pass
            it ends the call before that Record, and the next call gets it. The first
            Record of a call is always taken, also when it is larger. ``None`` is
            :py:data:`DEFAULT_STREAM_MAX_BYTES`, 256 KiB.
        value_type: The type that Workflow code decodes the value as. ``None`` takes the
            return annotation of ``function``, else the type of ``initial``, and no type
            when ``initial`` is ``None``. Client and Activity code get the value itself.

    Returns:
        The value, the number of Records, where the scan stopped, and why.

    Raises:
        TypeError: A function is a coroutine function, or the Stream is of the wrong kind.
        ValueError: A limit is out of range.
        temporalio.exceptions.StreamError: The Store failed the call, or an appender in a
            function failed its write.
        temporalio.exceptions.ApplicationError: A function raised, or returned an
            awaitable. Or the encoded result is over :py:data:`MAX_STREAM_RESULT_BYTES`,
            and the message names both sizes. In Workflow code it is also raised when the
            Worker's client has no Stream config, with the ``type`` ``StreamConfigError``.
            These are not retryable, so an uncaught one fails the Workflow.
        temporalio.workflow.NondeterminismError: In Workflow code, a replay of a call
            whose Store, Stream id or ``from_offset`` differs from the one in History.

    .. warning::
        This API is an experimental prototype and subject to change.
    """
    call = _StreamCall.check(
        _StreamCallKind.REDUCE,
        timeout=timeout,
        from_offset=from_offset,
        idle_timeout=idle_timeout,
        max_records=max_records,
        max_bytes=max_bytes,
        until=until,
        function=function,
        initial=initial,
        value_type=_fold_value_type(function, initial, value_type),
    )
    return await _run_stream_call(stream, call)
