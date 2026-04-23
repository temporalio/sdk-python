"""Shared data types for the pub/sub contrib module.

The user-facing ``data`` fields on :class:`PubSubItem` are
:class:`temporalio.api.common.v1.Payload` so that user codec chains
(encryption, PII-redaction, compression) apply per item. See
``DESIGN-v2.md`` §5 and ``docs/pubsub-payload-migration.md``.

The wire representation (``PublishEntry``, ``_WireItem``) uses
base64-encoded ``Payload.SerializeToString()`` bytes because the default
JSON payload converter cannot serialize a ``Payload`` embedded inside a
dataclass (it only special-cases top-level Payloads on signal/update
args). Round-trip validated in
``tests/contrib/pubsub/test_payload_roundtrip_prototype.py``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from temporalio.api.common.v1 import Payload


def _encode_payload(payload: Payload) -> str:
    """Wire format: base64(Payload.SerializeToString())."""
    return base64.b64encode(payload.SerializeToString()).decode("ascii")


def _decode_payload(wire: str) -> Payload:
    """Inverse of :func:`_encode_payload`."""
    payload = Payload()
    payload.ParseFromString(base64.b64decode(wire))
    return payload


@dataclass
class PubSubItem:
    """A single item in the pub/sub log.

    The ``data`` field is a :class:`temporalio.api.common.v1.Payload`
    as stored by the mixin and yielded by
    :meth:`PubSubClient.subscribe` when no ``result_type`` is given.
    When ``result_type`` is passed to ``subscribe``, ``data`` holds the
    decoded value of that type instead — the dataclass is typed as
    ``Any`` to accommodate both.

    The ``offset`` field is populated at poll time from the item's
    position in the global log.
    """

    topic: str
    data: Any
    offset: int = 0


@dataclass
class PublishEntry:
    """A single entry to publish via signal (wire type).

    ``data`` is base64-encoded ``Payload.SerializeToString()`` output —
    see module docstring for why a nested ``Payload`` cannot be used
    directly.
    """

    topic: str
    data: str


@dataclass
class PublishInput:
    """Signal payload: batch of entries to publish.

    Includes publisher_id and sequence to ensure exactly-once delivery.
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
    """Wire representation of a PubSubItem (base64 of serialized Payload)."""

    topic: str
    data: str
    offset: int = 0


@dataclass
class PollResult:
    """Update response: items matching the poll request.

    ``items`` use the wire representation. When ``more_ready`` is True,
    the response was truncated to stay within size limits and the
    subscriber should poll again immediately rather than applying a
    cooldown delay.
    """

    items: list[_WireItem] = field(default_factory=list)
    next_offset: int = 0
    more_ready: bool = False


@dataclass
class PubSubState:
    """Serializable snapshot of pub/sub state for continue-as-new.

    The containing workflow input must type the field as
    ``PubSubState | None``, not ``Any``, so the default data converter
    can reconstruct the dataclass from JSON.

    Log items use the wire representation for serialization stability.
    """

    log: list[_WireItem] = field(default_factory=list)
    base_offset: int = 0
    publisher_sequences: dict[str, int] = field(default_factory=dict)
    publisher_last_seen: dict[str, float] = field(default_factory=dict)
