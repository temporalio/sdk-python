"""Runtime for clients and workers. (experimental)

This module is currently experimental. The API may change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, Mapping, Optional, Union

import temporalio.bridge.runtime

_default_runtime: Optional[Runtime] = None


class Runtime:
    """Runtime for Temporal Python SDK.

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

    def __init__(self, *, telemetry: TelemetryConfig) -> None:
        """Create a default runtime with the given telemetry config.

        Each new runtime creates a new internal thread pool, so use sparingly.
        """
        self._core_runtime = temporalio.bridge.runtime.Runtime(
            telemetry=telemetry._to_bridge_config()
        )


@dataclass
class TelemetryFilter:
    """Filter for telemetry use."""

    core_level: str
    """Level for Core. Can be ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``, or
    ``TRACE``.
    """

    other_level: str
    """Level for non-Core. Can be ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``, or
    ``TRACE``.
    """

    def formatted(self) -> str:
        """Return a formatted form of this filter."""
        # We intentionally aren't using __str__ or __format__ so they can keep
        # their original dataclass impls
        return f"{self.other_level},temporal_sdk_core={self.core_level},temporal_client={self.core_level},temporal_sdk={self.core_level}"


@dataclass(frozen=True)
class TracingConfig:
    """Configuration for runtime tracing."""

    filter: Union[TelemetryFilter, str]
    """Filter for tracing. Can use :py:class:`TelemetryFilter` or raw string."""

    opentelemetry: OpenTelemetryConfig
    """Configuration for OpenTelemetry tracing collector."""

    def _to_bridge_config(self) -> temporalio.bridge.runtime.TracingConfig:
        return temporalio.bridge.runtime.TracingConfig(
            filter=self.filter
            if isinstance(self.filter, str)
            else self.filter.formatted(),
            opentelemetry=self.opentelemetry._to_bridge_config(),
        )


@dataclass(frozen=True)
class LoggingConfig:
    """Configuration for runtime logging."""

    filter: Union[TelemetryFilter, str]
    """Filter for logging. Can use :py:class:`TelemetryFilter` or raw string."""

    default: ClassVar[LoggingConfig]
    """Default logging configuration of Core WARN level and other ERROR
    level.
    """

    def _to_bridge_config(self) -> temporalio.bridge.runtime.LoggingConfig:
        return temporalio.bridge.runtime.LoggingConfig(
            filter=self.filter
            if isinstance(self.filter, str)
            else self.filter.formatted(),
            # Log forwarding not currently supported in Python
            forward=False,
        )


LoggingConfig.default = LoggingConfig(
    filter=TelemetryFilter(core_level="WARN", other_level="ERROR")
)


@dataclass(frozen=True)
class OpenTelemetryConfig:
    """Configuration for OpenTelemetry collector."""

    url: str
    headers: Optional[Mapping[str, str]] = None
    metric_periodicity: Optional[timedelta] = None

    def _to_bridge_config(self) -> temporalio.bridge.runtime.OpenTelemetryConfig:
        return temporalio.bridge.runtime.OpenTelemetryConfig(
            url=self.url,
            headers=self.headers or {},
            metric_periodicity_millis=None
            if not self.metric_periodicity
            else round(self.metric_periodicity.total_seconds() * 1000),
        )


@dataclass(frozen=True)
class PrometheusConfig:
    """Configuration for Prometheus metrics endpoint."""

    bind_address: str

    def _to_bridge_config(self) -> temporalio.bridge.runtime.PrometheusConfig:
        return temporalio.bridge.runtime.PrometheusConfig(
            bind_address=self.bind_address
        )


@dataclass(frozen=True)
class TelemetryConfig:
    """Configuration for Core telemetry."""

    tracing: Optional[TracingConfig] = None
    """Tracing configuration."""

    logging: Optional[LoggingConfig] = LoggingConfig.default
    """Logging configuration."""

    metrics: Optional[Union[OpenTelemetryConfig, PrometheusConfig]] = None
    """Metrics configuration."""

    def _to_bridge_config(self) -> temporalio.bridge.runtime.TelemetryConfig:
        return temporalio.bridge.runtime.TelemetryConfig(
            tracing=None if not self.tracing else self.tracing._to_bridge_config(),
            logging=None if not self.logging else self.logging._to_bridge_config(),
            metrics=None
            if not self.metrics
            else temporalio.bridge.runtime.MetricsConfig(
                opentelemetry=None
                if not isinstance(self.metrics, OpenTelemetryConfig)
                else self.metrics._to_bridge_config(),
                prometheus=None
                if not isinstance(self.metrics, PrometheusConfig)
                else self.metrics._to_bridge_config(),
            ),
        )
