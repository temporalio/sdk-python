"""The process side of a Stream read or fold that Workflow code waits on.

A Workflow's read runs in a Local Activity, and the Local Activity calls functions of
the Workflow. A function is a closure and can't be serialized, and the Records must
stay out of History. So both pass through the registry of this module, in the memory
of the Worker process. History keeps only a small marker with the range of offsets of
the read, and the value of a fold.

.. warning::
    This API is an experimental prototype and subject to change.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import struct
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

import temporalio.api.common.v1
import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.common
import temporalio.exceptions
from temporalio.workflow._streams import StreamReadStop

_MARKER_METADATA_KEY = "temporal-stream-read"
_MARKER_METADATA_VALUE = b"v2"
_MAX_PAGE_RECORDS = 1000
# A Store waits at most this long for one page.
_MAX_PAGE_WAIT = 60.0
_DIGEST_SIZE = 16


def _encode_bytes(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode()


def _decode_bytes(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value)


class _RecordDigest:
    """A SHA-256 over the offsets and bodies of Records, in the order that they come.

    A length before each part keeps two different splits of the same bytes apart.
    """

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def update(self, record: temporalio.api.stream.v1.StreamRecord) -> None:
        body = record.body.SerializeToString(deterministic=True)
        for part in (record.offset, body):
            self._hash.update(struct.pack(">Q", len(part)))
            self._hash.update(part)

    def digest(self) -> bytes:
        return self._hash.digest()[:_DIGEST_SIZE]


def _records_digest(
    records: Sequence[temporalio.api.stream.v1.StreamRecord],
) -> bytes:
    digest = _RecordDigest()
    for record in records:
        digest.update(record)
    return digest.digest()


@dataclasses.dataclass(frozen=True)
class _StreamReadMarker:
    """The result of a read's Local Activity, and so the only part of it in History."""

    read_id: str
    store: str
    stream_id: str
    from_offset: bytes | None
    last_offset: bytes | None
    record_count: int
    stop: StreamReadStop
    done: bool
    digest: bytes
    value: temporalio.api.common.v1.Payload | None = None
    """The value of a fold. A read of Records has none."""

    def to_raw_value(self) -> temporalio.common.RawValue:
        """Give the marker as a payload that a Worker can find in any activation."""
        data = {
            "read_id": self.read_id,
            "store": self.store,
            "stream_id": self.stream_id,
            "from_offset": _encode_bytes(self.from_offset),
            "last_offset": _encode_bytes(self.last_offset),
            "record_count": self.record_count,
            "stop": self.stop.value,
            "done": self.done,
            "digest": self.digest.hex(),
            "value": _encode_bytes(
                None
                if self.value is None
                else self.value.SerializeToString(deterministic=True)
            ),
        }
        return temporalio.common.RawValue(
            temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": b"json/plain",
                    _MARKER_METADATA_KEY: _MARKER_METADATA_VALUE,
                },
                data=json.dumps(data, separators=(",", ":"), sort_keys=True).encode(),
            )
        )

    @classmethod
    def from_payload(
        cls, payload: temporalio.api.common.v1.Payload
    ) -> _StreamReadMarker | None:
        """Give the marker in ``payload``, or ``None`` when the payload isn't one."""
        if payload.metadata.get(_MARKER_METADATA_KEY) != _MARKER_METADATA_VALUE:
            return None
        data = json.loads(payload.data)
        value = _decode_bytes(data["value"])
        return cls(
            read_id=data["read_id"],
            store=data["store"],
            stream_id=data["stream_id"],
            from_offset=_decode_bytes(data["from_offset"]),
            last_offset=_decode_bytes(data["last_offset"]),
            record_count=data["record_count"],
            stop=StreamReadStop(data["stop"]),
            done=data["done"],
            digest=bytes.fromhex(data["digest"]),
            value=None
            if value is None
            else temporalio.api.common.v1.Payload.FromString(value),
        )

    def describe_range(self) -> str:
        """Name the Stream and the range of the read, for an error message."""
        start = self.from_offset.hex() if self.from_offset else "the beginning"
        end = self.last_offset.hex() if self.last_offset else "the beginning"
        return (
            f"the Stream '{self.stream_id}' on the Store '{self.store}', "
            f"{self.record_count} Records after {start} up to {end}"
        )


@dataclasses.dataclass(frozen=True)
class _StreamFold:
    """The function and the start value of a fold."""

    function: Callable[[Any, Any], Any]
    initial: Any


@dataclasses.dataclass(eq=False)
class _PendingStreamRead:
    """One read of Workflow code, from the start of its Local Activity to its result."""

    to_record: Callable[[temporalio.api.stream.v1.StreamRecord], Any]
    """Turns a proto Record into the typed Record of the Workflow."""

    until: Callable[..., bool] | None
    """The predicate of the Workflow, or ``None``.

    A read calls it with the typed Record, and a fold with the value and the Record.
    """

    fold: _StreamFold | None = None
    """The fold, or ``None`` for a read that gives its Records."""

    _result: (
        tuple[_StreamReadMarker, list[temporalio.api.stream.v1.StreamRecord]] | None
    ) = None

    def fill(
        self,
        marker: _StreamReadMarker,
        records: list[temporalio.api.stream.v1.StreamRecord],
    ) -> None:
        """Keep the Records of ``marker``, whose payloads the codec already decoded."""
        # One assignment, so a reader on another thread sees both or neither.
        self._result = (marker, records)

    def records_for(
        self, marker: _StreamReadMarker
    ) -> list[temporalio.api.stream.v1.StreamRecord] | None:
        """Give the Records of ``marker``, or ``None`` when this read has none for it."""
        result = self._result
        if result is None or result[0] != marker:
            return None
        return result[1]


class _PendingStreamReads:
    """The pending reads of the process, by Workflow run id and read id.

    Workflow code adds and removes an entry on its own thread. The read Activity and the
    Worker look an entry up on the event loop of the Worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reads: dict[tuple[str, str], _PendingStreamRead] = {}

    def add(self, run_id: str, read_id: str, read: _PendingStreamRead) -> None:
        with self._lock:
            self._reads[(run_id, read_id)] = read

    def get(self, run_id: str, read_id: str) -> _PendingStreamRead | None:
        with self._lock:
            return self._reads.get((run_id, read_id))

    def remove(self, run_id: str, read_id: str, read: _PendingStreamRead) -> None:
        # A replay of the same run can add a new entry under the same key before the
        # code of an evicted run gets to its own removal.
        with self._lock:
            if self._reads.get((run_id, read_id)) is read:
                del self._reads[(run_id, read_id)]

    def drop_run(self, run_id: str) -> None:
        with self._lock:
            for key in [key for key in self._reads if key[0] == run_id]:
                del self._reads[key]

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._reads)


_PENDING_STREAM_READS = _PendingStreamReads()

_ReadPage = Callable[
    [temporalio.api.streamservice.v1.ReadRecordsRequest],
    Awaitable[temporalio.api.streamservice.v1.ReadRecordsResponse],
]


@dataclasses.dataclass(frozen=True)
class _ReadLimits:
    """The stops of a read, as the read Activity gets them."""

    max_records: int
    max_bytes: int | None
    idle_timeout: float | None
    """Seconds with no new Record before the read ends, or ``None``."""
    timeout: float
    """Seconds for the whole read."""


@dataclasses.dataclass(frozen=True)
class _ReadOutcome:
    records: list[temporalio.api.stream.v1.StreamRecord]
    """The Records of a read. A fold keeps none."""
    record_count: int
    last_offset: bytes | None
    digest: bytes
    stop: StreamReadStop
    done: bool
    value: Any
    """The value of a fold, or ``None`` for a read."""


class _ReadState:
    """What a read got so far, for one attempt of its Activity."""

    def __init__(self, read: _PendingStreamRead, from_offset: bytes | None) -> None:
        self._read = read
        self._digest = _RecordDigest()
        self.records: list[temporalio.api.stream.v1.StreamRecord] = []
        self.count = 0
        self.bytes = 0
        self.last_offset = from_offset
        self.value = read.fold.initial if read.fold is not None else None

    def take(self, record: temporalio.api.stream.v1.StreamRecord, size: int) -> bool:
        """Add ``record`` to the read, and give whether ``until`` is true after it."""
        self.count += 1
        self.bytes += size
        self.last_offset = record.offset
        self._digest.update(record)
        read = self._read
        if read.fold is None:
            self.records.append(record)
            if read.until is None:
                return False
            return bool(_call_workflow_function(read.until, read.to_record(record)))
        typed = read.to_record(record)
        self.value = _call_workflow_function(read.fold.function, self.value, typed)
        if read.until is None:
            return False
        return bool(_call_workflow_function(read.until, self.value, typed))

    def end(self, stop: StreamReadStop, *, done: bool = False) -> _ReadOutcome:
        return _ReadOutcome(
            records=self.records,
            record_count=self.count,
            last_offset=self.last_offset,
            digest=self._digest.digest(),
            stop=stop,
            done=done,
            value=self.value,
        )


def _call_workflow_function(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except Exception as err:
        # The Workflow gets this error from its Local Activity, in Workflow code.
        raise temporalio.exceptions.ApplicationError(
            str(err), type=type(err).__name__, non_retryable=True
        ) from err


async def _read_until(  # pyright: ignore[reportUnusedFunction]
    read_page: _ReadPage,
    *,
    ref: temporalio.api.stream.v1.StreamRef,
    from_offset: bytes | None,
    limits: _ReadLimits,
    read: _PendingStreamRead,
) -> _ReadOutcome:
    """Read pages until a stop of :py:class:`StreamReadStop` holds."""
    start = time.monotonic()
    deadline = start + limits.timeout
    # The idle stop counts from the last page that gave a Record.
    last_arrival = start
    state = _ReadState(read, from_offset)

    def idle(now: float) -> bool:
        return (
            limits.idle_timeout is not None
            and now - last_arrival >= limits.idle_timeout
        )

    while True:
        if state.count >= limits.max_records:
            return state.end(StreamReadStop.MAX_RECORDS)
        bytes_left = None
        if limits.max_bytes is not None:
            bytes_left = limits.max_bytes - state.bytes
            # Every Record has some bytes, so none fits in a cap that is used up.
            if bytes_left <= 0:
                return state.end(StreamReadStop.MAX_BYTES)
        now = time.monotonic()
        if now >= deadline:
            return state.end(
                StreamReadStop.IDLE if idle(now) else StreamReadStop.TIMEOUT
            )
        wait = min(_MAX_PAGE_WAIT, deadline - now)
        if limits.idle_timeout is not None:
            wait = min(wait, max(0.0, last_arrival + limits.idle_timeout - now))
        request = temporalio.api.streamservice.v1.ReadRecordsRequest(
            ref=ref,
            from_offset=state.last_offset or b"",
            max_records=min(limits.max_records - state.count, _MAX_PAGE_RECORDS),
            max_bytes=bytes_left or 0,
        )
        request.wait.FromTimedelta(timedelta(seconds=wait))
        response = await read_page(request)
        if response.records:
            last_arrival = time.monotonic()
        for index, record in enumerate(response.records):
            if state.count >= limits.max_records:
                return state.end(StreamReadStop.MAX_RECORDS)
            size = record.ByteSize()
            # A Store gives the first Record of a page even when it passes the cap, and
            # the read keeps it only when it is the first Record of the read.
            if (
                limits.max_bytes is not None
                and state.count > 0
                and state.bytes + size > limits.max_bytes
            ):
                return state.end(StreamReadStop.MAX_BYTES)
            matched = state.take(record, size)
            # A done page ends with the last Record of the Stream.
            done = response.done and index == len(response.records) - 1
            if matched:
                return state.end(StreamReadStop.MATCH, done=done)
            if done:
                return state.end(StreamReadStop.DONE, done=True)
        if response.done and not response.records:
            return state.end(StreamReadStop.DONE, done=True)
        if not response.records and idle(time.monotonic()):
            return state.end(StreamReadStop.IDLE)


async def _read_range(  # pyright: ignore[reportUnusedFunction]
    read_page: _ReadPage, marker: _StreamReadMarker
) -> list[temporalio.api.stream.v1.StreamRecord]:
    """Read the Records of ``marker`` again, and check them against it.

    Raises:
        RuntimeError: The Store failed, the Records are gone, or they don't match.
    """
    records: list[temporalio.api.stream.v1.StreamRecord] = []
    cursor = marker.from_offset
    ref = temporalio.api.stream.v1.StreamRef(id=marker.stream_id, store=marker.store)
    while len(records) < marker.record_count:
        left = marker.record_count - len(records)
        request = temporalio.api.streamservice.v1.ReadRecordsRequest(
            ref=ref,
            from_offset=cursor or b"",
            max_records=min(left, _MAX_PAGE_RECORDS),
        )
        try:
            response = await read_page(request)
        except Exception as err:
            raise RuntimeError(
                f"The replay of a Workflow read can't read {marker.describe_range()}: "
                f"{err}"
            ) from err
        if not response.records:
            raise RuntimeError(
                f"The replay of a Workflow read can't read {marker.describe_range()}: "
                f"the Store gave {len(records)} Records, and the Records after them are "
                "gone"
            )
        records.extend(list(response.records)[:left])
        cursor = records[-1].offset
    last_offset = records[-1].offset if records else marker.from_offset
    if last_offset != marker.last_offset or _records_digest(records) != marker.digest:
        raise RuntimeError(
            f"The replay of a Workflow read read {marker.describe_range()}, and the "
            "Records differ from the ones of the first run"
        )
    return records
