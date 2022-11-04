"""Telemetry for SDK Core. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import ClassVar, Mapping, Optional

import temporalio.bridge.temporal_sdk_bridge


def format_filter(core_level: str, other_level: str) -> str:
    """Helper to build a filter from Core and other level.

    Levels can be ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``, or ``TRACE``.

    Args:
        core_level: Level for SDK Core.
        other_level: Level for other things besides Core.

    Returns:
        Formatted string for use as a ``filter`` in telemetry configs.
    """
    return f"{other_level},temporal_sdk_core={core_level},temporal_client={core_level},temporal_sdk={core_level}"


@dataclass(frozen=True)
class TracingConfig:
    """Configuration for Core tracing."""

    filter: str
    """Filter string for tracing. Use :py:func:`format_filter`."""

    opentelemetry: OpenTelemetryConfig
    """Configuration for OpenTelemetry tracing collector."""


@dataclass(frozen=True)
class LoggingConfig:
    """Configuration for Core logging."""

    filter: str
    """Filter string for logging. Use :py:func:`format_filter`."""

    forward: bool = False
    """If true, logs are not on console but instead forwarded."""

    default: ClassVar[LoggingConfig]
    """Default logging configuration of Core WARN level and other ERROR
    level.
    """


LoggingConfig.default = LoggingConfig(filter=format_filter("WARN", "ERROR"))


@dataclass(frozen=True)
class MetricsConfig:
    """Configuration for Core metrics.

    One and only one of :py:attr:`opentelemetry` or :py:attr:`prometheus` must
    be set.
    """

    opentelemetry: Optional[OpenTelemetryConfig] = None
    """Configuration for OpenTelemetry metrics collector."""

    prometheus: Optional[PrometheusConfig] = None
    """Configuration for Prometheus metrics endpoint."""


@dataclass(frozen=True)
class OpenTelemetryConfig:
    """Configuration for OpenTelemetry collector."""

    url: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class PrometheusConfig:
    """Configuration for Prometheus metrics endpoint."""

    bind_address: str


@dataclass(frozen=True)
class TelemetryConfig:
    """Configuration for Core telemetry."""

    tracing: Optional[TracingConfig] = None
    """Tracing configuration."""

    logging: Optional[LoggingConfig] = LoggingConfig.default
    """Logging configuration."""

    metrics: Optional[PrometheusConfig] = None
    """Metrics configuration."""


_inited = False


def init_telemetry(
    config: TelemetryConfig, *, warn_if_already_inited: bool = True
) -> bool:
    """Initialize telemetry with the given configuration.

    This must be called before any Temporal client is created. Does nothing if
    already called.

    .. warning::
        This API is not stable and may change in a future release.

    Args:
        config: Telemetry config.
        warn_if_already_inited: If True and telemetry is already initialized,
            this will emit a warning.
    """
    global _inited
    if _inited:
        if warn_if_already_inited:
            warnings.warn(
                "Telemetry initialization already called, ignoring successive calls"
            )
        return False
    temporalio.bridge.temporal_sdk_bridge.init_telemetry(config)
    _inited = True
    return True
