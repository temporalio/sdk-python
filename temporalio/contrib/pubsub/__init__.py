"""Pub/sub support for Temporal workflows.

This module provides a reusable pub/sub pattern where a workflow acts as a
message broker. External clients (activities, starters, other services) publish
and subscribe through the workflow handle using Temporal primitives.

Payloads are Temporal ``Payload`` values. Publishing values are
converted to ``Payload`` per item by the client's payload converter;
the codec chain (encryption, PII-redaction, compression) runs once on
the surrounding signal/update envelope rather than per item. Subscribers
yield raw ``Payload`` by default, or decode each item to a concrete
type via ``subscribe(result_type=T)``.
"""

from temporalio.contrib.pubsub._broker import PubSub
from temporalio.contrib.pubsub._client import PubSubClient
from temporalio.contrib.pubsub._types import (
    PollInput,
    PollResult,
    PublishEntry,
    PublishInput,
    PubSubItem,
    PubSubState,
)

__all__ = [
    "PollInput",
    "PollResult",
    "PubSub",
    "PubSubClient",
    "PubSubItem",
    "PubSubState",
    "PublishEntry",
    "PublishInput",
]
