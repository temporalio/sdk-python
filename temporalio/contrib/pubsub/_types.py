"""Shared data types for the pub/sub contrib module."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


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
    """A single entry to publish (used in batch signals)."""

    topic: str
    data: bytes


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
    timeout: float = 300.0


@dataclass
class PollResult:
    """Update response: items matching the poll request."""

    items: list[PubSubItem] = field(default_factory=list)
    next_offset: int = 0


class PubSubState(BaseModel):
    """Serializable snapshot of pub/sub state for continue-as-new.

    This is a Pydantic model (not a dataclass) so that Pydantic-based data
    converters can properly reconstruct it. The containing workflow input
    must type the field as ``PubSubState | None``, not ``Any``.
    """

    log: list[PubSubItem] = Field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = Field(default_factory=dict)
