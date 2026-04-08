"""Shared data types for the pub/sub contrib module."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


def encode_data(data: bytes) -> str:
    """Encode bytes to base64 string for wire format."""
    return base64.b64encode(data).decode("ascii")


def decode_data(data: str) -> bytes:
    """Decode base64 string from wire format to bytes."""
    return base64.b64decode(data)


@dataclass
class PubSubItem:
    """A single item in the pub/sub log.

    The global offset is not stored on the item — it is the item's index
    in the log (adjusted by base_offset). See DESIGN-ADDENDUM-TOPICS.md.
    """

    topic: str
    data: bytes


@dataclass
class PublishEntry:
    """A single entry to publish via signal (wire type).

    The ``data`` field is a base64-encoded string for cross-language
    compatibility over Temporal's JSON payload converter.
    """

    topic: str
    data: str  # base64-encoded bytes


@dataclass
class PublishInput:
    """Signal payload: batch of entries to publish.

    Includes publisher_id and sequence for exactly-once deduplication.
    See DESIGN-ADDENDUM-DEDUP.md.
    """

    items: list[PublishEntry] = field(default_factory=list)
    publisher_id: str = ""
    sequence: int = 0


@dataclass
class PollInput:
    """Update payload: request to poll for new items."""

    topics: list[str] = field(default_factory=list)
    from_offset: int = 0


@dataclass
class _WireItem:
    """Wire representation of a PubSubItem (base64 data)."""

    topic: str
    data: str  # base64-encoded bytes


@dataclass
class PollResult:
    """Update response: items matching the poll request.

    Items use base64-encoded data for cross-language wire compatibility.
    """

    items: list[_WireItem] = field(default_factory=list)
    next_offset: int = 0


class PubSubState(BaseModel):
    """Serializable snapshot of pub/sub state for continue-as-new.

    This is a Pydantic model (not a dataclass) so that Pydantic-based data
    converters can properly reconstruct it. The containing workflow input
    must type the field as ``PubSubState | None``, not ``Any``.

    The log items use base64-encoded data for serialization stability.
    """

    log: list[_WireItem] = Field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = Field(default_factory=dict)
    publisher_last_seen: dict[str, float] = Field(default_factory=dict)
