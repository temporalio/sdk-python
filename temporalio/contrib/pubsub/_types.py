"""Shared data types for the pub/sub contrib module."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel


@dataclass
class PubSubItem:
    """A single item in the pub/sub log."""

    #@AGENT: why are we repeating the topic and storing the offset? why not have a list of dicts? do we need this full granularity to preserve global ordering? it seems expensive. is there anything more efficient. Perhaps we should back off of the global ordering guarantee - let's consider it as a design trade-off
    offset: int
    topic: str
    data: bytes


@dataclass
class PublishEntry:
    """A single entry to publish (used in batch signals)."""
    #@AGENT: this feels verbose. should we have lists by topic? or do we need the full granularity to preserve ordering
    topic: str
    data: bytes


@dataclass
class PublishInput:
    """Signal payload: batch of entries to publish."""

    items: list[PublishEntry] = field(default_factory=list)


@dataclass
class PollInput:
    """Update payload: request to poll for new items."""

    topics: list[str] = field(default_factory=list)
    from_offset: int = 0
    #@AGENT: I think we should list the offset for each topic individually, the global offset is not exposed to the world
    timeout: float = 300.0


@dataclass
class PollResult:
    """Update response: items matching the poll request."""

    items: list[PubSubItem] = field(default_factory=list)
    next_offset: int = 0


#@AGENT: let's check to make sure this really needs to be a pydantic - but only after we confirm the data model
class PubSubState(BaseModel):
    """Serializable snapshot of pub/sub state for continue-as-new.

    This is a Pydantic model (not a dataclass) so that Pydantic-based data
    converters can properly reconstruct it. The containing workflow input
    must type the field as ``PubSubState | None``, not ``Any``.
    """
    #@AGENT: should we have some sort of versioning, or does pydantic take care of that
    log: list[PubSubItem] = []
