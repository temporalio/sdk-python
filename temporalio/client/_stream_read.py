"""The process side of a Stream read that Workflow code waits on.

A Workflow's read runs in a Local Activity, and the Local Activity calls a predicate
of the Workflow. A predicate is a closure and can't be serialized, and the Records
must stay out of History. So both pass through the registry of this module, in the
memory of the Worker process. History keeps only a small marker with the range of
offsets of the read.

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

_MARKER_METADATA_KEY = "temporal-stream-read"
_MARKER_METADATA_VALUE = b"v1"
_MAX_PAGE_RECORDS = 1000
# A Store waits at most this long for one page.
_MAX_PAGE_WAIT = timedelta(seconds=60)
_DIGEST_SIZE = 16


def _encode_offset(offset: bytes | None) -> str | None:
    return None if offset is None else base64.b64encode(offset).decode()


def _decode_offset(offset: str | None) -> bytes | None:
    return None if offset is None else base64.b64decode(offset)


def _records_digest(
    records: Sequence[temporalio.api.stream.v1.StreamRecord],
) -> bytes:
    """Give the first bytes of a SHA-256 over the offsets and bodies of ``records``.

    A length before each part keeps two different splits of the same bytes apart.
    """
    digest = hashlib.sha256()
    for record in records:
        body = record.body.SerializeToString(deterministic=True)
        for part in (record.offset, body):
            digest.update(struct.pack(">Q", len(part)))
            digest.update(part)
    return digest.digest()[:_DIGEST_SIZE]


@dataclasses.dataclass(frozen=True)
class _StreamReadMarker:
    """The result of a read's Local Activity, and so the only part of it in History."""

    read_id: str
    store: str
    stream_id: str
    from_offset: bytes | None
    last_offset: bytes | None
    record_count: int
    matched: bool
    done: bool
    digest: bytes

    @classmethod
    def for_records(
        cls,
        *,
        read_id: str,
        store: str,
        stream_id: str,
        from_offset: bytes | None,
        records: Sequence[temporalio.api.stream.v1.StreamRecord],
        matched: bool,
        done: bool,
    ) -> _StreamReadMarker:
        """Build the marker of a read that gave ``records``."""
        return cls(
            read_id=read_id,
            store=store,
            stream_id=stream_id,
            from_offset=from_offset,
            last_offset=records[-1].offset if records else from_offset,
            record_count=len(records),
            matched=matched,
            done=done,
            digest=_records_digest(records),
        )

    def to_raw_value(self) -> temporalio.common.RawValue:
        """Give the marker as a payload that a Worker can find in any activation."""
        data = {
            "read_id": self.read_id,
            "store": self.store,
            "stream_id": self.stream_id,
            "from_offset": _encode_offset(self.from_offset),
            "last_offset": _encode_offset(self.last_offset),
            "record_count": self.record_count,
            "matched": self.matched,
            "done": self.done,
            "digest": self.digest.hex(),
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
        return cls(
            read_id=data["read_id"],
            store=data["store"],
            stream_id=data["stream_id"],
            from_offset=_decode_offset(data["from_offset"]),
            last_offset=_decode_offset(data["last_offset"]),
            record_count=data["record_count"],
            matched=data["matched"],
            done=data["done"],
            digest=bytes.fromhex(data["digest"]),
        )

    def describe_range(self) -> str:
        """Name the Stream and the range of the read, for an error message."""
        start = self.from_offset.hex() if self.from_offset else "the beginning"
        end = self.last_offset.hex() if self.last_offset else "the beginning"
        return (
            f"the Stream '{self.stream_id}' on the Store '{self.store}', "
            f"{self.record_count} Records after {start} up to {end}"
        )


@dataclasses.dataclass(eq=False)
class _PendingStreamRead:
    """One read of Workflow code, from the start of its Local Activity to its result."""

    until: Callable[[Any], bool] | None
    """The predicate of the Workflow, or ``None``."""

    to_record: Callable[[temporalio.api.stream.v1.StreamRecord], Any]
    """Turns a proto Record into the typed Record of the Workflow."""

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

    def __len__(self) -> int:
        with self._lock:
            return len(self._reads)


_PENDING_STREAM_READS = _PendingStreamReads()

_ReadPage = Callable[
    [temporalio.api.streamservice.v1.ReadRecordsRequest],
    Awaitable[temporalio.api.streamservice.v1.ReadRecordsResponse],
]


@dataclasses.dataclass(frozen=True)
class _ReadUntilResult:
    records: list[temporalio.api.stream.v1.StreamRecord]
    matched: bool
    done: bool


async def _read_until(  # pyright: ignore[reportUnusedFunction]
    read_page: _ReadPage,
    *,
    ref: temporalio.api.stream.v1.StreamRef,
    from_offset: bytes | None,
    max_records: int,
    timeout: timedelta,
    read: _PendingStreamRead,
) -> _ReadUntilResult:
    """Read pages until the predicate of ``read`` is true, or a limit stops the read."""
    deadline = time.monotonic() + timeout.total_seconds()
    records: list[temporalio.api.stream.v1.StreamRecord] = []
    cursor = from_offset
    while len(records) < max_records:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            break
        request = temporalio.api.streamservice.v1.ReadRecordsRequest(
            ref=ref,
            from_offset=cursor or b"",
            max_records=min(max_records - len(records), _MAX_PAGE_RECORDS),
        )
        request.wait.FromTimedelta(min(timedelta(seconds=time_left), _MAX_PAGE_WAIT))
        response = await read_page(request)
        page = list(response.records)[: max_records - len(records)]
        for index, record in enumerate(page):
            records.append(record)
            if read.until is not None and _call_until(read, record):
                # A done page ends with the last Record, so a match on it is the end too.
                last = index == len(response.records) - 1
                return _ReadUntilResult(records, True, response.done and last)
        if response.done and len(page) == len(response.records):
            return _ReadUntilResult(records, False, True)
        if page:
            cursor = page[-1].offset
    return _ReadUntilResult(records, False, False)


def _call_until(
    read: _PendingStreamRead, record: temporalio.api.stream.v1.StreamRecord
) -> bool:
    assert read.until is not None
    try:
        return bool(read.until(read.to_record(record)))
    except Exception as err:
        # The Workflow gets this error from its Local Activity, in Workflow code.
        raise temporalio.exceptions.ApplicationError(
            str(err), type=type(err).__name__, non_retryable=True
        ) from err


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
