"""Steps that chain over a Stream and run as one call.

.. warning::
    This module is experimental and subject to change.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Generic, Protocol, TypeVar

import temporalio.common
from temporalio.nexus.system.stream_service import (
    StreamRead,
    StreamRecord,
    StreamReduce,
)

BodyT = TypeVar("BodyT")
ItemT = TypeVar("ItemT")
NewItemT = TypeVar("NewItemT")
ValueT = TypeVar("ValueT")

# The value that the fold of a chain carries from Record to Record. It holds the caller's
# value, the count of each `limit` step, whether the chain ended, and how many items reached
# the end. A Local Activity can run its fold again, so no step keeps a count elsewhere.
_State = tuple[Any, tuple[int, ...], bool, int]


class _Stream(Protocol[BodyT]):
    """The three calls of a client ``Stream`` and of a Workflow ``Stream``."""

    async def take(
        self,
        timeout: timedelta,
        until: Callable[[StreamRecord[BodyT]], bool] | None = None,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamRead[BodyT]: ...

    async def filter(
        self,
        predicate: Callable[[StreamRecord[BodyT]], bool],
        timeout: timedelta,
        until: Callable[[StreamRecord[BodyT]], bool] | None = None,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamRead[BodyT]: ...

    async def reduce(
        self,
        function: Callable[[ValueT, StreamRecord[BodyT]], ValueT],
        initial: ValueT,
        timeout: timedelta,
        until: Callable[[ValueT, StreamRecord[BodyT]], bool] | None = None,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamReduce[ValueT]: ...


class _StepKind(enum.Enum):
    FILTER = "filter"
    MAP = "map"
    TAKE_UNTIL = "take_until"
    LIMIT = "limit"


@dataclasses.dataclass(frozen=True)
class _Step:
    kind: _StepKind
    function: Callable[[Any], Any] | None = None
    count: int = 0


def pipeline(stream: _Stream[BodyT]) -> Pipeline[StreamRecord[BodyT]]:
    """Start a chain of steps over a Stream.

    Args:
        stream: A client ``Stream`` or a Workflow ``Stream``.

    Returns:
        A chain with no steps, whose items are the Records of the Stream.

    .. warning::
        This API is experimental and subject to change.
    """
    return Pipeline(stream, ())


class Pipeline(Generic[ItemT]):
    """A chain of steps over a Stream. Its end runs the whole chain as one call.

    Each step gives a new chain and runs nothing. An item is a ``StreamRecord`` until the
    first :py:meth:`map`, and the value of that map after it. An end makes one ``take``,
    ``filter`` or ``reduce`` call of the Stream, so in Workflow code the chain is one Local
    Activity and one marker in History. Client, Activity and Workflow code get the same
    result.

    The functions of the steps must be plain functions that keep no state. In Workflow code
    a Local Activity can run them again after a failure, with the same function objects.

    ``last_offset`` of each result is where the scan stopped, also when a
    :py:meth:`take_until` or a :py:meth:`limit` ended the chain. Give it as the
    ``from_offset`` of the next end, so a loop loses and repeats no Record.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(self, stream: _Stream[Any], steps: tuple[_Step, ...]) -> None:
        """Make a chain. Use :py:func:`pipeline` to start one."""
        self._stream = stream
        self._steps = steps

    def _then(self, step: _Step) -> Pipeline[Any]:
        return Pipeline(self._stream, (*self._steps, step))

    def filter(self, predicate: Callable[[ItemT], bool]) -> Pipeline[ItemT]:
        """Keep the items that ``predicate`` accepts."""
        return self._then(_Step(_StepKind.FILTER, predicate))

    def map(self, function: Callable[[ItemT], NewItemT]) -> Pipeline[NewItemT]:
        """Turn each item into the value of ``function``.

        In Workflow code, :py:meth:`collect` decodes the items as the return annotation of
        the last ``map``. Give one when the items are not plain JSON values.
        """
        return self._then(_Step(_StepKind.MAP, function))

    def take_until(self, predicate: Callable[[ItemT], bool]) -> Pipeline[ItemT]:
        """End the chain after the first item that ``predicate`` accepts.

        That item still goes on to the next step.
        """
        return self._then(_Step(_StepKind.TAKE_UNTIL, predicate))

    def limit(self, count: int) -> Pipeline[ItemT]:
        """End the chain after ``count`` items passed this step.

        Raises:
            ValueError: ``count`` is less than 1.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        return self._then(_Step(_StepKind.LIMIT, count=count))

    def records(
        self: Pipeline[StreamRecord[BodyT]],
        timeout: timedelta,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> Awaitable[StreamRead[BodyT]]:
        """Give the Records that reach the end of the chain.

        It makes one ``filter`` call of the Stream, or one ``take`` call when the chain
        keeps every Record. The filters become the call's ``predicate``, the
        :py:meth:`take_until` its ``until``, and the :py:meth:`limit` its ``max_records``,
        with no count of their own. A limit over ``max_records``, or over 1,000 when
        ``max_records`` is ``None``, ends at that count. ``max_records`` and ``max_bytes``
        count the Records that the chain keeps. A limit ends the call with the stop
        ``MaxRecords``, and a take_until with ``Match``.

        In Workflow code the Records of the result go into History, so their encoded size
        must fit in 1 MiB. ``max_bytes``, 256 KiB by default, keeps them under it.

        The chain may hold any number of :py:meth:`filter` steps, at most one
        :py:meth:`take_until`, and at most one :py:meth:`limit` as its last step.

        Args:
            timeout: As in ``take`` and ``filter`` of the Stream.
            from_offset: As in ``take`` and ``filter`` of the Stream.
            idle_timeout: As in ``take`` and ``filter`` of the Stream.
            max_records: As in ``take`` and ``filter`` of the Stream.
            max_bytes: As in ``take`` and ``filter`` of the Stream.

        Returns:
            The kept Records, where the scan stopped, and why.

        Raises:
            TypeError: The chain has another step, such as a ``map``. The message names
                that step. Such a chain ends with :py:meth:`collect` or :py:meth:`reduce`.
        """
        filters: list[Callable[[Any], Any]] = []
        until: Callable[[StreamRecord[BodyT]], bool] | None = None
        limit: int | None = None
        for index, step in enumerate(self._steps):
            name = _misfit(step, index, len(self._steps), until is not None)
            if name is not None:
                raise TypeError(
                    f"records() can't run this chain, since its step {name} doesn't fit. "
                    "It runs only filter steps, one take_until and a limit as the last "
                    "step. End the chain with collect() or reduce()."
                )
            if step.kind is _StepKind.FILTER:
                assert step.function is not None
                filters.append(step.function)
            elif step.kind is _StepKind.TAKE_UNTIL:
                assert step.function is not None
                until = _until_after(tuple(filters), step.function)
            else:
                limit = step.count
        if limit is not None:
            ceiling = (
                max_records
                if max_records is not None
                else temporalio.common.DEFAULT_STREAM_MAX_RECORDS
            )
            max_records = min(limit, ceiling)
        stream: _Stream[BodyT] = self._stream
        if not filters:
            return stream.take(
                timeout,
                until=until,
                from_offset=from_offset,
                idle_timeout=idle_timeout,
                max_records=max_records,
                max_bytes=max_bytes,
            )
        return stream.filter(
            _all_of(tuple(filters)),
            timeout,
            until=until,
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    async def reduce(
        self,
        function: Callable[[ValueT, ItemT], ValueT],
        initial: ValueT,
        timeout: timedelta,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamReduce[ValueT]:
        """Fold the items that reach the end of the chain into one value.

        It makes one ``reduce`` call of the Stream. ``value = function(value, item)`` runs
        for each item that reaches the end. ``function`` must not change ``initial`` or a
        value in place. ``count`` of the result is the number of those items.
        ``max_records`` and ``max_bytes`` count every Record that the scan looks at. A
        take_until or a limit that ends the chain ends the call with the stop ``Match``.

        In Workflow code the value goes into History with the counts of the chain, so its
        encoded size must fit in 1 MiB. Workflow code decodes the value as the return
        annotation of ``function``, else as the type of ``initial``.

        Args:
            function: Gives the next value from the value so far and an item.
            initial: The value before the first item.
            timeout: As in ``reduce`` of the Stream.
            from_offset: As in ``reduce`` of the Stream.
            idle_timeout: As in ``reduce`` of the Stream.
            max_records: As in ``reduce`` of the Stream.
            max_bytes: As in ``reduce`` of the Stream.

        Returns:
            The value, the number of items that reached the end, where the scan stopped,
            and why.
        """
        return await self._fold(
            function,
            initial,
            _value_type(function, initial),
            timeout=timeout,
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    async def collect(
        self,
        timeout: timedelta,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamReduce[list[ItemT]]:
        """Give the items that reach the end of the chain as a list.

        A chain with a :py:meth:`map` makes one ``reduce`` call of the Stream, as
        :py:meth:`reduce` does. A Record can't be the value of a fold, so a chain with no
        ``map`` runs as :py:meth:`records`, with one ``take`` or ``filter`` call.

        In Workflow code the list goes into History, so its encoded size must fit in 1 MiB.
        The items of a map are decoded as the return annotation of its function. Records
        go into History as :py:meth:`records` puts them there.

        Args:
            timeout: As in the call of the Stream.
            from_offset: As in the call of the Stream.
            idle_timeout: As in the call of the Stream.
            max_records: As in the call of the Stream.
            max_bytes: As in the call of the Stream.

        Returns:
            The items, their number, where the scan stopped, and why.

        Raises:
            TypeError: The chain has no ``map`` and :py:meth:`records` can't run it.
        """
        maps = [step for step in self._steps if step.kind is _StepKind.MAP]
        if not maps:
            try:
                running = typing.cast("Pipeline[StreamRecord[Any]]", self).records(
                    timeout,
                    from_offset=from_offset,
                    idle_timeout=idle_timeout,
                    max_records=max_records,
                    max_bytes=max_bytes,
                )
            except TypeError as err:
                raise TypeError(
                    "collect() of Records runs as records(), since a Record can't be the "
                    f"value of a fold. Add a map before collect(). {err}"
                ) from None
            read = await running
            return StreamReduce(
                value=typing.cast("list[ItemT]", list(read.records)),
                count=len(read.records),
                last_offset=read.last_offset,
                stop=read.stop,
                done=read.done,
            )
        assert maps[-1].function is not None
        item_type = _return_type(maps[-1].function)
        empty: list[ItemT] = []
        return await self._fold(
            _append_item,
            empty,
            types.GenericAlias(list, (Any if item_type is None else item_type,)),
            timeout=timeout,
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    async def count(
        self,
        timeout: timedelta,
        from_offset: bytes | None = None,
        idle_timeout: timedelta | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> StreamReduce[int]:
        """Count the items that reach the end of the chain.

        It makes one ``reduce`` call of the Stream, as :py:meth:`reduce` does. In Workflow
        code the count goes into History with the counts of the chain, a few bytes that fit
        well in the 1 MiB of a result.

        Args:
            timeout: As in ``reduce`` of the Stream.
            from_offset: As in ``reduce`` of the Stream.
            idle_timeout: As in ``reduce`` of the Stream.
            max_records: As in ``reduce`` of the Stream.
            max_bytes: As in ``reduce`` of the Stream.

        Returns:
            The number of items, where the scan stopped, and why.
        """
        return await self._fold(
            _add_one,
            0,
            int,
            timeout=timeout,
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    async def _fold(
        self,
        function: Callable[[ValueT, Any], ValueT],
        initial: ValueT,
        value_type: Any,
        *,
        timeout: timedelta,
        from_offset: bytes | None,
        idle_timeout: timedelta | None,
        max_records: int | None,
        max_bytes: int | None,
    ) -> StreamReduce[ValueT]:
        steps = self._steps
        limits = sum(1 for step in steps if step.kind is _StepKind.LIMIT)

        def fold(state: _State, record: StreamRecord[Any]) -> _State:
            value, counts, ended, reached = state
            item: Any = record
            next_counts = list(counts)
            limit_index = 0
            for step in steps:
                if step.kind is _StepKind.FILTER:
                    assert step.function is not None
                    if not step.function(item):
                        return value, tuple(next_counts), ended, reached
                elif step.kind is _StepKind.MAP:
                    assert step.function is not None
                    item = step.function(item)
                elif step.kind is _StepKind.TAKE_UNTIL:
                    assert step.function is not None
                    if step.function(item):
                        ended = True
                else:
                    next_counts[limit_index] += 1
                    if next_counts[limit_index] >= step.count:
                        ended = True
                    limit_index += 1
            return function(value, item), tuple(next_counts), ended, reached + 1

        # The call decodes the value of History as the return annotation of `fold`, so the
        # caller's value keeps its type in Workflow code.
        fold.__annotations__ = {
            "return": types.GenericAlias(
                tuple, (value_type, tuple[int, ...], bool, int)
            )
        }
        start: _State = (initial, (0,) * limits, False, 0)
        result = await self._stream.reduce(
            fold,
            start,
            timeout,
            until=_has_ended,
            from_offset=from_offset,
            idle_timeout=idle_timeout,
            max_records=max_records,
            max_bytes=max_bytes,
        )
        value, _, _, reached = result.value
        return StreamReduce(
            value=value,
            count=reached,
            last_offset=result.last_offset,
            stop=result.stop,
            done=result.done,
        )


def _misfit(step: _Step, index: int, steps: int, has_until: bool) -> str | None:
    """Name the step when ``records`` can't run it, else give ``None``."""
    if step.kind is _StepKind.MAP:
        return "map"
    if step.kind is _StepKind.TAKE_UNTIL and has_until:
        return "take_until, the second one"
    if step.kind is _StepKind.LIMIT and index != steps - 1:
        return "limit, which isn't the last step"
    return None


def _all_of(
    predicates: tuple[Callable[[Any], Any], ...],
) -> Callable[[StreamRecord[Any]], bool]:
    def predicate(record: StreamRecord[Any]) -> bool:
        return all(check(record) for check in predicates)

    return predicate


def _until_after(
    filters: tuple[Callable[[Any], Any], ...], until: Callable[[Any], Any]
) -> Callable[[StreamRecord[Any]], bool]:
    # The take_until sees only the Records that pass the filters before it.
    def predicate(record: StreamRecord[Any]) -> bool:
        return all(check(record) for check in filters) and bool(until(record))

    return predicate


def _has_ended(state: _State, record: StreamRecord[Any]) -> bool:
    del record
    return state[2]


def _append_item(value: list[Any], item: Any) -> list[Any]:
    return [*value, item]


def _add_one(value: int, item: Any) -> int:
    del item
    return value + 1


def _return_type(function: Callable[..., Any]) -> Any:
    try:
        return typing.get_type_hints(function).get("return")
    except Exception:
        return None


def _value_type(function: Callable[..., Any], initial: Any) -> Any:
    found = _return_type(function)
    if found is not None:
        return found
    return type(initial) if initial is not None else Any
