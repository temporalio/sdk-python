"""Telemetry for SDK Core. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Mapping, Optional, Type

import temporalio.bridge.temporal_sdk_bridge

_default_runtime: Optional[Runtime] = None


class Runtime:
    """Runtime for SDK Core.

    Users are encouraged to use :py:meth:`default`. It can be set with
    :py:meth:`set_default`. Every time a new runtime is created, a new internal
    thread pool is created.
    """

    @staticmethod
    def default() -> Runtime:
        """Get the default runtime, creating if not already created.

        If the default runtime needs to be different, it should be done with
        :py:meth:`set_default` before this is called or ever used.

        Returns:
            The default runtime.
        """
        global _default_runtime
        if not _default_runtime:
            _default_runtime = Runtime(telemetry=TelemetryConfig())
        return _default_runtime

    @staticmethod
    def set_default(runtime: Runtime, *, error_if_already_set: bool = True) -> None:
        """Set the default runtime to the given runtime.

        This should be called before any Temporal client is created, but can
        change the existing one. Any clients and workers created with the
        previous runtime will stay on that runtime.

        Args:
            runtime: The runtime to set.
            error_if_already_set: If True and default is already set, this will
                raise a RuntimeError.
        """
        global _default_runtime
        if _default_runtime and error_if_already_set:
            raise RuntimeError("Runtime default already set")
        _default_runtime = runtime

    @staticmethod
    def _raise_in_thread(thread_id: int, exc_type: Type[BaseException]) -> bool:
        """Internal helper for raising an exception in thread."""
        return temporalio.bridge.temporal_sdk_bridge.raise_in_thread(
            thread_id, exc_type
        )

    def __init__(self, *, telemetry: TelemetryConfig) -> None:
        """Create a default runtime with the given telemetry config.

        Each new runtime creates a new internal thread pool, so use sparingly.
        """
        self._ref = temporalio.bridge.temporal_sdk_bridge.init_runtime(telemetry)


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
    metric_periodicity_millis: Optional[int] = None


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

    metrics: Optional[MetricsConfig] = None
    """Metrics configuration."""
