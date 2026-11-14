"""The scan of the take, filter and reduce calls, for client, Activity and Workflow code.

Client and Activity code run the scan with the pages of the client ``Stream.read``. Workflow
code runs it in a Local Activity that reads the Store, and the result of that Local Activity is
the result of the call. The functions of the caller can't be serialized, so they reach the
Local Activity through the registry of this module, in the memory of the Worker process.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import inspect
import json
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any, Generic, Protocol, TypeVar

import temporalio.activity
import temporalio.api.common.v1
import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.nexus.system.stream_service.client
from temporalio.nexus.system._support import _StreamCall, _StreamCallKind
from temporalio.nexus.system.stream_service.models import (
    StreamRead,
    StreamReadStop,
    StreamRecord,
    StreamReduce,
)
from temporalio.nexus.system.stream_service.workflow._resources.stream import (
    Stream as _WorkflowStream,
)

from ._stream_store import StreamStore, _store_page_reader

_RESULT_METADATA_KEY = "temporal-stream-read"
_RESULT_METADATA_VALUE = b"v3"
_RECORDS_MESSAGE_TYPE = b"temporal.api.streamservice.v1.ReadRecordsResponse"
_MAX_PAGE_RECORDS = 1000
# A Store waits at most this long for one page.
_MAX_PAGE_WAIT = 60.0


class _CallFlush(Protocol):
    """A writer that a function of a call used, such as a ``BufferedAppender``."""

    async def flush(self) -> object: ...


# The writers that the functions of one take, filter or reduce call used, so the call writes
# their Records before it returns. It is unset outside such a function.
_CALL_FLUSHES: contextvars.ContextVar[list[_CallFlush] | None] = contextvars.ContextVar(
    "temporal_stream_call_flushes", default=None
)

# The size of each Record of one page that the scan of a client or an Activity reads, by its
# offset. The scan sets it around one `Stream.read`, and the client's Store call fills it. Only
# that call has the proto Records, and `max_bytes` counts their encoded size.
_PAGE_SIZES: contextvars.ContextVar[dict[bytes, int] | None] = contextvars.ContextVar(
    "temporal_stream_page_sizes", default=None
)


def _note_page_sizes(  # pyright: ignore[reportUnusedFunction]
    response: temporalio.api.streamservice.v1.ReadRecordsResponse,
) -> None:
    """Give the scan that waits for this page the size of each of its Records."""
    sizes = _PAGE_SIZES.get()
    if sizes is None:
        return
    for record in response.records:
        sizes[record.offset] = record.ByteSize()


_ToRecord = Callable[[temporalio.api.stream.v1.StreamRecord], Any]
_RecordT = TypeVar("_RecordT")


@dataclasses.dataclass(frozen=True)
class _PageRequest:
    """One page that the scan asks for."""

    from_offset: bytes | None
    max_records: int
    max_bytes: int
    """The cap on the bytes of the page, or 0 for no cap."""
    wait: timedelta


class _Pages(ABC, Generic[_RecordT]):
    """Reads the pages of one scan, and tells what the scan needs of each Record."""

    @abstractmethod
    async def read(self, request: _PageRequest) -> tuple[Sequence[_RecordT], bool]:
        """Give the Records of the next page, and whether it ends a closed Stream."""

    @abstractmethod
    def offset(self, record: _RecordT) -> bytes:
        """Give the offset of a Record of the last page."""

    @abstractmethod
    def size(self, record: _RecordT) -> int:
        """Give the encoded size of a Record of the last page, after the codec."""

    @abstractmethod
    def typed(self, record: _RecordT) -> Any:
        """Give the Record that the caller's functions get."""


class _StorePages(_Pages[temporalio.api.stream.v1.StreamRecord]):
    """The pages of the Local Activity of Workflow code, read from its Store.

    The Records stay proto Records, so the result in History keeps the bodies as the Store
    gave them.
    """

    def __init__(
        self,
        store: StreamStore,
        data_converter: temporalio.converter.DataConverter,
        ref: temporalio.api.stream.v1.StreamRef,
        to_record: _ToRecord,
    ) -> None:
        self._read_page = _store_page_reader(store, data_converter)
        self._ref = ref
        self._to_record = to_record

    async def read(
        self, request: _PageRequest
    ) -> tuple[Sequence[temporalio.api.stream.v1.StreamRecord], bool]:
        message = temporalio.api.streamservice.v1.ReadRecordsRequest(
            ref=self._ref,
            from_offset=request.from_offset or b"",
            max_records=request.max_records,
            max_bytes=request.max_bytes,
        )
        message.wait.FromTimedelta(request.wait)
        response = await self._read_page(message)
        return response.records, response.done

    def offset(self, record: temporalio.api.stream.v1.StreamRecord) -> bytes:
        return record.offset

    def size(self, record: temporalio.api.stream.v1.StreamRecord) -> int:
        return record.ByteSize()

    def typed(self, record: temporalio.api.stream.v1.StreamRecord) -> Any:
        return self._to_record(record)


class _ClientPages(_Pages[StreamRecord[Any]]):
    """The pages of client and Activity code, each one ``read`` of a client ``Stream``."""

    def __init__(
        self, stream: temporalio.nexus.system.stream_service.client.Stream[Any]
    ) -> None:
        self._stream = stream
        self._sizes: dict[bytes, int] = {}

    async def read(
        self, request: _PageRequest
    ) -> tuple[Sequence[StreamRecord[Any]], bool]:
        sizes: dict[bytes, int] = {}
        token = _PAGE_SIZES.set(sizes)
        try:
            response = await self._stream.read(
                from_offset=request.from_offset,
                max_records=request.max_records,
                wait=request.wait,
                max_bytes=request.max_bytes or None,
            )
        finally:
            _PAGE_SIZES.reset(token)
        self._sizes = sizes
        return response.records, response.done

    def offset(self, record: StreamRecord[Any]) -> bytes:
        return record.offset

    def size(self, record: StreamRecord[Any]) -> int:
        # Only a Store call measures a Record. An interceptor that answers a read itself
        # gives Records that none measured, so they count no bytes.
        return self._sizes.get(record.offset, 0)

    def typed(self, record: StreamRecord[Any]) -> Any:
        return record


def _encode_offset(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode()


def _decode_offset(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value)


def _call_function(
    label: str, function: Callable[..., Any], *args: Any, in_local_activity: bool
) -> Any:
    try:
        result = function(*args)
    except temporalio.exceptions.ApplicationError:
        raise
    except Exception as err:
        if not in_local_activity:
            raise
        # Workflow code gets this error from the Local Activity, at the `await` of its call.
        raise temporalio.exceptions.ApplicationError(
            str(err), type=type(err).__name__, non_retryable=True
        ) from err
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise temporalio.exceptions.ApplicationError(
            f"The {label} of a Stream call returned an awaitable. It must be a plain "
            "function that returns its answer.",
            type="TypeError",
            non_retryable=True,
        )
    return result


@dataclasses.dataclass
class _ScanOutcome(Generic[_RecordT]):
    kept: list[_RecordT]
    """The Records that the call gives back. A fold keeps none."""
    count: int
    """How many Records the call kept or folded."""
    last_offset: bytes | None
    stop: StreamReadStop
    done: bool
    value: Any
    """The value of a fold, or ``None``."""


class _ScanState(Generic[_RecordT]):
    """What a scan got so far."""

    def __init__(
        self, call: _StreamCall, pages: _Pages[_RecordT], *, in_local_activity: bool
    ) -> None:
        self._call = call
        self._pages = pages
        self._in_local_activity = in_local_activity
        self.kept: list[_RecordT] = []
        self.count = 0
        self.bytes = 0
        self.last_offset = call.from_offset
        self.value = call.initial if call.kind is _StreamCallKind.REDUCE else None

    def _call_function(
        self, label: str, function: Callable[..., Any], *args: Any
    ) -> Any:
        return _call_function(
            label, function, *args, in_local_activity=self._in_local_activity
        )

    def bytes_left(self) -> int:
        return self._call.max_bytes - self.bytes

    def look(self, record: _RecordT, *, ends_stream: bool) -> StreamReadStop | None:
        """Look at the next Record, and give the stop that holds after it, if any."""
        call = self._call
        typed: Any = None
        if call.kind is not _StreamCallKind.TAKE or call.until is not None:
            typed = self._pages.typed(record)
        keep = True
        if call.predicate is not None:
            keep = bool(self._call_function("predicate", call.predicate, typed))
        size = self._pages.size(record)
        # The first Record of a call is taken even when it passes the cap.
        if keep and self.count > 0 and self.bytes + size > call.max_bytes:
            return StreamReadStop.MaxBytes
        self.last_offset = self._pages.offset(record)
        if keep:
            self.count += 1
            self.bytes += size
            if call.function is not None:
                self.value = self._call_function(
                    "function", call.function, self.value, typed
                )
            else:
                self.kept.append(record)
        if call.until is not None:
            args = (typed,) if call.function is None else (self.value, typed)
            if self._call_function("until", call.until, *args):
                return StreamReadStop.Match
        if ends_stream:
            return StreamReadStop.Done
        if self.count >= call.max_records:
            return StreamReadStop.MaxRecords
        return None

    def end(
        self, stop: StreamReadStop, *, done: bool = False
    ) -> _ScanOutcome[_RecordT]:
        return _ScanOutcome(
            kept=self.kept,
            count=self.count,
            last_offset=self.last_offset,
            stop=stop,
            done=done,
            value=self.value,
        )


async def _scan(
    pages: _Pages[_RecordT], *, call: _StreamCall, in_local_activity: bool
) -> _ScanOutcome[_RecordT]:
    """Read pages after ``call.from_offset`` until a stop of ``StreamReadStop``."""
    idle_timeout = call.idle_timeout.total_seconds()
    start = time.monotonic()
    deadline = start + call.timeout.total_seconds()
    # The idle stop counts from the last page that gave a Record.
    last_arrival = start
    state = _ScanState(call, pages, in_local_activity=in_local_activity)
    # A filter counts only its kept Records, so the Store's cap of a page can't serve it.
    page_cap = call.kind is not _StreamCallKind.FILTER

    def idle(now: float) -> bool:
        return now - last_arrival >= idle_timeout

    while True:
        if page_cap and state.count > 0 and state.bytes_left() <= 0:
            # Every Record has some bytes, so none fits in a cap that is used up.
            return state.end(StreamReadStop.MaxBytes)
        now = time.monotonic()
        if now >= deadline:
            return state.end(
                StreamReadStop.Idle if idle(now) else StreamReadStop.Timeout
            )
        wait = min(
            _MAX_PAGE_WAIT, deadline - now, max(0.0, last_arrival + idle_timeout - now)
        )
        records, done = await pages.read(
            _PageRequest(
                from_offset=state.last_offset,
                max_records=min(call.max_records - state.count, _MAX_PAGE_RECORDS)
                if page_cap
                else _MAX_PAGE_RECORDS,
                max_bytes=max(state.bytes_left(), 1) if page_cap else 0,
                wait=timedelta(seconds=wait),
            )
        )
        if records:
            last_arrival = time.monotonic()
        for index, record in enumerate(records):
            # A done page ends with the last Record of the Stream.
            ends_stream = done and index == len(records) - 1
            stop = state.look(record, ends_stream=ends_stream)
            if stop is not None:
                return state.end(
                    stop, done=ends_stream and stop is not StreamReadStop.MaxBytes
                )
        if done and not records:
            return state.end(StreamReadStop.Done, done=True)
        if not records and idle(time.monotonic()):
            return state.end(StreamReadStop.Idle)


async def _scan_and_flush(
    pages: _Pages[_RecordT], *, call: _StreamCall, in_local_activity: bool
) -> _ScanOutcome[_RecordT]:
    """Run :py:func:`_scan`, then write the Records that the caller's functions appended.

    In Workflow code the result goes into History only after those Records are in the Store.
    """
    flushes: list[_CallFlush] = []
    token = _CALL_FLUSHES.set(flushes)
    try:
        outcome = await _scan(pages, call=call, in_local_activity=in_local_activity)
    except Exception:
        # The error of the scan wins over an error of the write.
        await _flush_all(flushes, raise_error=False)
        raise
    finally:
        _CALL_FLUSHES.reset(token)
    await _flush_all(flushes, raise_error=True)
    return outcome


async def _flush_all(flushes: list[_CallFlush], *, raise_error: bool) -> None:
    for flush in flushes:
        try:
            await flush.flush()
        except Exception:
            if raise_error:
                raise


@dataclasses.dataclass(frozen=True)
class _StreamCallResult:
    """The result of one call, as the payload that History keeps for a Workflow."""

    kind: _StreamCallKind
    store: str
    stream_id: str
    from_offset: bytes | None
    last_offset: bytes | None
    count: int
    stop: StreamReadStop
    done: bool
    records: list[temporalio.api.stream.v1.StreamRecord]
    """The kept Records of a take or a filter, with their bodies."""
    value: temporalio.api.common.v1.Payload | None
    """The value of a fold."""

    def to_payload(self) -> temporalio.api.common.v1.Payload:
        """Give the result as one payload. The bodies stay in their binary form."""
        header = {
            "kind": self.kind.value,
            "store": self.store,
            "stream_id": self.stream_id,
            "from_offset": _encode_offset(self.from_offset),
            "last_offset": _encode_offset(self.last_offset),
            "count": self.count,
            "stop": self.stop.name,
            "done": self.done,
        }
        parts = [
            temporalio.api.common.v1.Payload(
                metadata={"encoding": b"json/plain"},
                data=json.dumps(header, separators=(",", ":"), sort_keys=True).encode(),
            )
        ]
        if self.value is not None:
            parts.append(self.value)
        else:
            parts.append(
                temporalio.api.common.v1.Payload(
                    metadata={
                        "encoding": b"binary/protobuf",
                        "messageType": _RECORDS_MESSAGE_TYPE,
                    },
                    data=temporalio.api.streamservice.v1.ReadRecordsResponse(
                        records=self.records
                    ).SerializeToString(),
                )
            )
        return temporalio.api.common.v1.Payload(
            metadata={
                "encoding": b"binary/plain",
                _RESULT_METADATA_KEY: _RESULT_METADATA_VALUE,
            },
            data=temporalio.api.common.v1.Payloads(payloads=parts).SerializeToString(),
        )

    @classmethod
    def from_payload(
        cls, payload: temporalio.api.common.v1.Payload
    ) -> _StreamCallResult | None:
        """Give the result in ``payload``, or ``None`` when the payload isn't one."""
        if payload.metadata.get(_RESULT_METADATA_KEY) != _RESULT_METADATA_VALUE:
            return None
        header_part, body = temporalio.api.common.v1.Payloads.FromString(
            payload.data
        ).payloads
        header = json.loads(header_part.data)
        kind = _StreamCallKind(header["kind"])
        records: list[temporalio.api.stream.v1.StreamRecord] = []
        value = None
        if kind is _StreamCallKind.REDUCE:
            value = body
        else:
            records = list(
                temporalio.api.streamservice.v1.ReadRecordsResponse.FromString(
                    body.data
                ).records
            )
        return cls(
            kind=kind,
            store=header["store"],
            stream_id=header["stream_id"],
            from_offset=_decode_offset(header["from_offset"]),
            last_offset=_decode_offset(header["last_offset"]),
            count=header["count"],
            stop=StreamReadStop[header["stop"]],
            done=header["done"],
            records=records,
            value=value,
        )


def _result_payload(
    *,
    call: _StreamCall,
    store: str,
    stream_id: str,
    outcome: _ScanOutcome[temporalio.api.stream.v1.StreamRecord],
    payload_converter: temporalio.converter.PayloadConverter,
) -> temporalio.api.common.v1.Payload:
    """Give the result for History as a payload, or raise when it is over the limit."""
    payload = _StreamCallResult(
        kind=call.kind,
        store=store,
        stream_id=stream_id,
        from_offset=call.from_offset,
        last_offset=outcome.last_offset,
        count=outcome.count,
        stop=outcome.stop,
        done=outcome.done,
        records=outcome.kept,
        value=payload_converter.to_payload(outcome.value)
        if call.kind is _StreamCallKind.REDUCE
        else None,
    ).to_payload()
    size = payload.ByteSize()
    limit = temporalio.common.MAX_STREAM_RESULT_BYTES
    if size > limit:
        raise temporalio.exceptions.ApplicationError(
            f"The result of the {call.kind.value} of the Stream '{stream_id}' is {size} "
            f"bytes encoded, over the limit of {limit} bytes. Lower "
            "max_bytes or max_records, or fold to a smaller value.",
            type="StreamResultTooLarge",
            non_retryable=True,
        )
    return payload


@dataclasses.dataclass(eq=False)
class _PendingStreamCall:
    """One call of Workflow code, from the start of its Local Activity to its result.

    It holds only what can't go into the Local Activity's input.
    """

    call: _StreamCall
    """The checked call, with the caller's functions."""

    store: StreamStore
    """The Store that the call reads."""

    ref: temporalio.api.stream.v1.StreamRef

    to_record: _ToRecord
    """Turns a proto Record into the typed Record of the Workflow."""


class _PendingStreamCalls:
    """The pending calls of the process, by Workflow run id and the call's number.

    Workflow code adds and removes an entry on its own thread. The Local Activity looks an
    entry up on the event loop of the Worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], _PendingStreamCall] = {}

    def add(self, run_id: str, call_id: str, call: _PendingStreamCall) -> None:
        with self._lock:
            self._calls[(run_id, call_id)] = call

    def get(self, run_id: str, call_id: str) -> _PendingStreamCall | None:
        with self._lock:
            return self._calls.get((run_id, call_id))

    def remove(self, run_id: str, call_id: str, call: _PendingStreamCall) -> None:
        # A replay of the same run can add a new entry under the same key before the
        # code of an evicted run gets to its own removal.
        with self._lock:
            if self._calls.get((run_id, call_id)) is call:
                del self._calls[(run_id, call_id)]

    def drop_run(self, run_id: str) -> None:
        with self._lock:
            for key in [key for key in self._calls if key[0] == run_id]:
                del self._calls[key]

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._calls)


_PENDING_STREAM_CALLS = _PendingStreamCalls()

_STREAM_CALL_ACTIVITY = "__temporal_stream_call"


@temporalio.activity.defn(name=_STREAM_CALL_ACTIVITY)
async def _stream_call_activity(  # pyright: ignore[reportUnusedFunction]
    call_id: str,
) -> temporalio.common.RawValue:
    """Run the scan of one call of Workflow code, and give its result."""
    info = temporalio.activity.info()
    pending = _PENDING_STREAM_CALLS.get(info.workflow_run_id or "", call_id)
    if pending is None:
        # The functions of the caller live in its process, so only a Local Activity of
        # that Workflow can run the scan.
        raise temporalio.exceptions.ApplicationError(
            f"The Stream call {call_id} has no pending call in this process. Only the "
            "take, filter and reduce of a Workflow Stream run this Activity.",
            non_retryable=True,
        )
    # The codec decodes the bodies of each page, so the caller's functions get its values.
    outcome = await _scan_and_flush(
        _StorePages(
            pending.store,
            temporalio.activity.client().data_converter,
            pending.ref,
            pending.to_record,
        ),
        call=pending.call,
        in_local_activity=True,
    )
    return temporalio.common.RawValue(
        _result_payload(
            call=pending.call,
            store=pending.store.name,
            stream_id=pending.ref.id,
            outcome=outcome,
            payload_converter=temporalio.activity.payload_converter(),
        )
    )


def _client_stream(
    stream: Any,
) -> temporalio.nexus.system.stream_service.client.Stream[Any]:
    if isinstance(stream, temporalio.nexus.system.stream_service.client.Stream):
        return stream
    if isinstance(stream, _WorkflowStream) and temporalio.activity.in_activity():
        # A Workflow Stream outside Workflow code is one that a function of a call or an
        # Activity got, so it reads through the Activity's client.
        return temporalio.activity.client().get_stream(
            stream.ref, body_type=stream.body_type
        )
    raise TypeError(
        "A Workflow Stream can be read only in Workflow code or an Activity. Use "
        "client.get_stream for a client Stream."
    )


async def _stream_call_outside_workflow(  # pyright: ignore[reportUnusedFunction]
    stream: Any, call: _StreamCall
) -> StreamRead[Any] | StreamReduce[Any]:
    """Run the scan of a call of client or Activity code with the pages of ``Stream.read``.

    The result doesn't go into History, so it has no size limit and isn't encoded.
    """
    outcome = await _scan_and_flush(
        _ClientPages(_client_stream(stream)), call=call, in_local_activity=False
    )
    if call.kind is _StreamCallKind.REDUCE:
        return StreamReduce(
            value=outcome.value,
            count=outcome.count,
            last_offset=outcome.last_offset,
            stop=outcome.stop,
            done=outcome.done,
        )
    return StreamRead(
        records=outcome.kept,
        last_offset=outcome.last_offset,
        stop=outcome.stop,
        done=outcome.done,
    )
