"""Payload size limit configuration and related types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayloadLimitsConfig:
    """Configuration for when payload sizes exceed limits."""

    memo_size_warning: int = 2 * 1024
    """The limit (in bytes) at which a memo size warning is logged. Set to 0 to disable."""

    payload_size_warning: int = 512 * 1024
    """The limit (in bytes) at which a payload size warning is logged. Set to 0 to disable."""


class PayloadSizeWarning(RuntimeWarning):
    """The size of payloads is above the warning limit.

    .. deprecated::
        Payload size warnings are no longer raised through the :mod:`warnings` module. This
        symbol is retained for backwards compatibility and is no longer raised.
    """
