"""Pub/sub support for Temporal workflows.

This module provides a reusable pub/sub pattern where a workflow acts as a
message broker. External clients (activities, starters, other services) publish
and subscribe through the workflow handle using Temporal primitives.

Payloads are opaque bytes. Base64 encoding is used on the wire for
cross-language compatibility, but users work with native byte types.
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
