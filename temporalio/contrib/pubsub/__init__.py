"""Pub/sub support for Temporal workflows.

This module provides a reusable pub/sub pattern where a workflow acts as a
message broker. External clients (activities, starters, other services) publish
and subscribe through the workflow handle using Temporal primitives.

Payloads are Temporal ``Payload`` values. Publishing values go through
the client's data converter (including any configured codec chain);
subscribers can yield raw ``Payload`` or request a concrete type via
``subscribe(result_type=T)``.
"""

from temporalio.contrib.pubsub._client import PubSubClient
from temporalio.contrib.pubsub._mixin import PubSubMixin
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
    "PubSubClient",
    "PubSubItem",
    "PubSubMixin",
    "PubSubState",
    "PublishEntry",
    "PublishInput",
]
