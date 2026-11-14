"""The scan of the take, filter and reduce calls, for client, Activity and Workflow code.

Client and Activity code run the scan straight against the Store. Workflow code runs it in a
Local Activity, and the result of that Local Activity is the result of the call. The
functions of the caller can't be serialized, so they reach the Local Activity through the
registry of this module, in the memory of the Worker process.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import temporalio.activity
import temporalio.api.common.v1
import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.nexus.system
import temporalio.nexus.system.stream_service.client
from temporalio.nexus.system.stream_service.models import (
    ReadRecordsRequest,
    StreamRecord,
)
from temporalio.workflow._streams import (
    MAX_STREAM_RESULT_BYTES,
    StreamRead,
    StreamReadStop,
    StreamReduce,
    _StreamCall,
    _StreamCallKind,
)

from ._client import Client
from ._stream_appender import _CALL_APPENDERS, _flush_call_appenders
from ._stream_store import (
    _STREAM_SERVICE,
    StreamStore,
    _activity_target,
    _store_page_reader,
)

_RESULT_METADATA_KEY = "temporal-stream-read"
_RESULT_METADATA_VALUE = b"v3"
_RECORDS_MESSAGE_TYPE = b"temporal.api.streamservice.v1.ReadRecordsResponse"
_MAX_PAGE_RECORDS = 1000
# A Store waits at most this long for one page.
_MAX_PAGE_WAIT = 60.0

_ReadPage = Callable[
    [temporalio.api.streamservice.v1.ReadRecordsRequest],
    Awaitable[temporalio.api.streamservice.v1.ReadRecordsResponse],
]
_ToRecord = Callable[[temporalio.api.stream.v1.StreamRecord], Any]


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
class _ScanOutcome:
    kept: list[temporalio.api.stream.v1.StreamRecord]
    """The Records that the call gives back. A fold keeps none."""
    count: int
    """How many Records the call kept or folded."""
    last_offset: bytes | None
    stop: StreamReadStop
    done: bool
    value: Any
    """The value of a fold, or ``None``."""


class _ScanState:
    """What a scan got so far."""

    def __init__(
        self, call: _StreamCall, to_record: _ToRecord, *, in_local_activity: bool
    ) -> None:
        self._call = call
        self._to_record = to_record
        self._in_local_activity = in_local_activity
        self.kept: list[temporalio.api.stream.v1.StreamRecord] = []
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

    def look(
        self, record: temporalio.api.stream.v1.StreamRecord, *, ends_stream: bool
    ) -> StreamReadStop | None:
        """Look at the next Record, and give the stop that holds after it, if any."""
        call = self._call
        typed: Any = None
        if call.kind is not _StreamCallKind.TAKE or call.until is not None:
            typed = self._to_record(record)
        keep = True
        if call.predicate is not None:
            keep = bool(self._call_function("predicate", call.predicate, typed))
        size = record.ByteSize()
        # The first Record of a call is taken even when it passes the cap.
        if keep and self.count > 0 and self.bytes + size > call.max_bytes:
            return StreamReadStop.MaxBytes
        self.last_offset = record.offset
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

    def end(self, stop: StreamReadStop, *, done: bool = False) -> _ScanOutcome:
        return _ScanOutcome(
            kept=self.kept,
            count=self.count,
            last_offset=self.last_offset,
            stop=stop,
            done=done,
            value=self.value,
        )


async def _scan(
    read_page: _ReadPage,
    *,
    ref: temporalio.api.stream.v1.StreamRef,
    call: _StreamCall,
    to_record: _ToRecord,
    in_local_activity: bool,
) -> _ScanOutcome:
    """Read pages after ``call.from_offset`` until a stop of :py:class:`StreamReadStop`."""
    idle_timeout = call.idle_timeout.total_seconds()
    start = time.monotonic()
    deadline = start + call.timeout.total_seconds()
    # The idle stop counts from the last page that gave a Record.
    last_arrival = start
    state = _ScanState(call, to_record, in_local_activity=in_local_activity)
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
        request = temporalio.api.streamservice.v1.ReadRecordsRequest(
            ref=ref,
            from_offset=state.last_offset or b"",
            max_records=min(call.max_records - state.count, _MAX_PAGE_RECORDS)
            if page_cap
            else _MAX_PAGE_RECORDS,
            max_bytes=max(state.bytes_left(), 1) if page_cap else 0,
        )
        request.wait.FromTimedelta(timedelta(seconds=wait))
        response = await read_page(request)
        if response.records:
            last_arrival = time.monotonic()
        for index, record in enumerate(response.records):
            # A done page ends with the last Record of the Stream.
            ends_stream = response.done and index == len(response.records) - 1
            stop = state.look(record, ends_stream=ends_stream)
            if stop is not None:
                return state.end(
                    stop, done=ends_stream and stop is not StreamReadStop.MaxBytes
                )
        if response.done and not response.records:
            return state.end(StreamReadStop.Done, done=True)
        if not response.records and idle(time.monotonic()):
            return state.end(StreamReadStop.Idle)


async def _scan_and_flush(
    read_page: _ReadPage,
    *,
    ref: temporalio.api.stream.v1.StreamRef,
    call: _StreamCall,
    to_record: _ToRecord,
    in_local_activity: bool,
) -> _ScanOutcome:
    """Run :py:func:`_scan`, then write the Records that the caller's functions appended.

    In Workflow code the result goes into History only after those Records are in the Store.
    """
    appenders: list[Any] = []
    token = _CALL_APPENDERS.set(appenders)
    try:
        outcome = await _scan(
            read_page,
            ref=ref,
            call=call,
            to_record=to_record,
            in_local_activity=in_local_activity,
        )
    except Exception:
        # The error of the scan wins over an error of the write.
        await _flush_call_appenders(appenders, raise_error=False)
        raise
    finally:
        _CALL_APPENDERS.reset(token)
    await _flush_call_appenders(appenders, raise_error=True)
    return outcome


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
    outcome: _ScanOutcome,
    payload_converter: temporalio.converter.PayloadConverter,
) -> temporalio.api.common.v1.Payload:
    """Give the result as a payload, or raise when it is over the limit."""
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
    if size > MAX_STREAM_RESULT_BYTES:
        raise temporalio.exceptions.ApplicationError(
            f"The result of the {call.kind.value} of the Stream '{stream_id}' is {size} "
            f"bytes encoded, over the limit of {MAX_STREAM_RESULT_BYTES} bytes. Lower "
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
            f"The Stream call {call_id} has no pending call in this process. Only "
            "take_stream, filter_stream and reduce_stream in Workflow code run this "
            "Activity.",
            non_retryable=True,
        )
    # The codec decodes the bodies of each page, so the caller's functions get its values.
    outcome = await _scan_and_flush(
        _store_page_reader(pending.store, temporalio.activity.client().data_converter),
        ref=pending.ref,
        call=pending.call,
        to_record=pending.to_record,
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


def _stream_client(stream: Any) -> Client:
    if isinstance(stream, temporalio.nexus.system.stream_service.client.Stream):
        client = getattr(getattr(stream, "_nexus_client", None), "_client", None)
        if isinstance(client, Client):
            return client
        raise TypeError("The client Stream has no Temporal client")
    if temporalio.activity.in_activity():
        # A Workflow Stream outside Workflow code is one that a function of a call or an
        # Activity got, so the Activity's client reaches its Store.
        return temporalio.activity.client()
    raise TypeError(
        "A Workflow Stream can be read only in Workflow code or an Activity. Use "
        "client.get_stream for a client Stream."
    )


async def _stream_call_outside_workflow(  # pyright: ignore[reportUnusedFunction]
    stream: Any, call: _StreamCall
) -> StreamRead[Any] | StreamReduce[Any]:
    """Run the scan of a call of client or Activity code straight against the Store."""
    client = _stream_client(stream)
    stream_config = client.config(active_config=True).get("stream_config")
    if stream_config is None:
        raise temporalio.exceptions.StreamConfigError(
            "This client has no Stream Store. Give one in "
            "stream_config=StreamConfig(stores=[...])."
        )
    request = ReadRecordsRequest(ref=stream.ref, from_offset=call.from_offset)
    data_converter = client.data_converter
    serialization_context = temporalio.nexus.system._get_serialization_context(
        _STREAM_SERVICE.name, "ReadRecords", request
    )
    if serialization_context is not None:
        data_converter = data_converter.with_context(serialization_context)
    payload_converter = temporalio.nexus.system._get_payload_converter(
        data_converter.payload_converter, data_converter.failure_converter
    )
    message = payload_converter._to_transfer_value(request)
    assert isinstance(message, temporalio.api.streamservice.v1.ReadRecordsRequest)
    store = stream_config._store_for(stream.ref, _activity_target())
    record_type: Any = StreamRecord[stream.body_type]  # type: ignore[name-defined]

    def to_record(record: temporalio.api.stream.v1.StreamRecord) -> Any:
        return payload_converter._from_transfer_value(record, record_type)

    outcome = await _scan_and_flush(
        _store_page_reader(store, data_converter),
        ref=message.ref,
        call=call,
        to_record=to_record,
        in_local_activity=False,
    )
    # The limit is the same as in Workflow code, so a call gives the same result anywhere.
    _result_payload(
        call=call,
        store=store.name,
        stream_id=message.ref.id,
        outcome=outcome,
        payload_converter=data_converter.payload_converter,
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
        records=[to_record(record) for record in outcome.kept],
        last_offset=outcome.last_offset,
        stop=outcome.stop,
        done=outcome.done,
    )
