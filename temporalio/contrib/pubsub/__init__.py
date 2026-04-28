"""Pub/sub support for Temporal workflows.

.. warning::
    This package is experimental and may change in future versions.

This module provides a reusable pub/sub pattern where a workflow acts as a
message broker, using Temporal's native messaging primitives — signals for
publishing and updates for long-poll subscription — to implement the broker
without external infrastructure.

See :py:class:`PubSub` for the workflow-side broker and
:py:class:`PubSubClient` for the external client interface.
"""

from temporalio.contrib.pubsub._broker import PubSub
from temporalio.contrib.pubsub._client import PubSubClient
from temporalio.contrib.pubsub._types import (
    PollInput,
    PollResult,
    PublishEntry,
    PublisherState,
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
    "PublisherState",
]
