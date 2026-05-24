"""Type definitions for ``temporalio.streams``.

Mirror the server-side proto messages (see
``temporal/chasm/lib/stream/proto/v1/`` in the temporal-server tree) but
stay native-Python so the SDK doesn't take a runtime dep on the
chasm-lib proto bindings.  A transport layer translates between these
types and the wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class StreamCloseReason(Enum):
    """Why a stream was closed.  Mirror of
    ``temporal.server.chasm.lib.stream.proto.v1.StreamCloseReason``."""

    UNSPECIFIED = 0
    EXPLICIT = 1
    OWNER_WORKFLOW_DONE = 2
    DELETED = 3
    NAMESPACE_DELETED = 4


@dataclass
class PublishItem:
    """One item in a publish batch.

    The ``data`` bytes are codec-applied (see contrib's "codec runs once
    on the envelope" rule, which carries over to native streams).
    """

    data: bytes
    topic: str = ""


@dataclass
class PublishResult:
    """The committed offset range of a successful publish.

    On dedup-replay (a retry with the same ``(publisher_id, sequence)``),
    the server returns the SAME ``(first_offset, item_count)`` it
    returned the first time around.  See
    ``native-streams/exactly-once.html#contract``.
    """

    first_offset: int
    item_count: int


@dataclass
class StreamState:
    """Read-only snapshot of a stream's chasm component state."""

    stream_id: str
    head_offset: int
    base_offset: int
    committed_txn_id: int
    closed: bool
    created_by: str = ""
    created_time: datetime | None = None
    closed_by: str = ""
    closed_time: datetime | None = None
    close_reason: StreamCloseReason = StreamCloseReason.UNSPECIFIED
    owner_workflow_id: str = ""
    owner_run_id: str = ""
    retention_max_bytes: int = 0
    retention_max_items: int = 0
    segment_max_items: int = 0
    segment_max_bytes: int = 0


@dataclass
class StreamDescription:
    """What :py:meth:`StreamClient.describe_stream` returns."""

    state: StreamState
    inflight_count: int
    subscription_count: int
    publisher_count: int
    stuck_subscription_count: int = 0
    force_truncated_subscription_count: int = 0


@dataclass
class StreamSummary:
    """One entry in a :py:meth:`StreamClient.list_streams` page."""

    stream_id: str
    head_offset: int
    base_offset: int
    closed: bool
    created_time: datetime | None = None
    closed_time: datetime | None = None


@dataclass
class ListStreamsPage:
    """A page of :py:class:`StreamSummary` plus the cursor for the next."""

    streams: list[StreamSummary] = field(default_factory=list)
    next_page_token: bytes | None = None


@dataclass
class TruncateResult:
    """What :py:meth:`StreamClient.truncate` returns."""

    new_base_offset: int
    force_truncated_subscription_count: int


@dataclass
class CreateStreamOptions:
    """Options for :py:meth:`StreamClient.create_stream`.

    Per-stream TTL is intentionally excluded — see
    ``native-streams/open-questions.html`` Q6.  Use namespace retention
    policy for time-based cleanup; use ``retention_max_bytes`` and
    ``retention_max_items`` for size-based caps.
    """

    retention_max_bytes: int = 0
    retention_max_items: int = 0
    segment_max_items: int = 0
    segment_max_bytes: int = 0
    publisher_ttl: timedelta | None = None
    owner_workflow_id: str = ""
    owner_run_id: str = ""
    created_by: str = ""
