"""Payload size limit configuration and related types."""

from __future__ import annotations

from dataclasses import dataclass

import temporalio.exceptions


@dataclass(frozen=True)
class PayloadLimitsConfig:
    """Configuration for when payload sizes exceed limits."""

    memo_size_warning: int = 2 * 1024
    """The limit (in bytes) at which a memo size warning is logged."""

    payload_size_warning: int = 512 * 1024
    """The limit (in bytes) at which a payload size warning is logged."""


class PayloadSizeWarning(RuntimeWarning):
    """The size of payloads is above the warning limit."""


class _PayloadSizeError(temporalio.exceptions.TemporalError):  # type:ignore[reportUnusedClass]
    """Error raised when payloads size exceeds payload size limits."""

    def __init__(self, message: str):
        """Initialize a payloads size error."""
        super().__init__(message)
        self._message = message

    @property
    def message(self) -> str:
        """Message."""
        return self._message


@dataclass(frozen=True)
class _ServerPayloadErrorLimits:  # type:ignore[reportUnusedClass]
    """Error limits for payloads as described by the Temporal server."""

    memo_size_error: int
    """The limit (in bytes) at which a memo size error is raised."""

    payload_size_error: int
    """The limit (in bytes) at which a payload size error is raised."""
