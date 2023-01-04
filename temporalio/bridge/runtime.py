"""Telemetry for SDK Core. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Type

import temporalio.bridge.temporal_sdk_bridge


class Runtime:
    """Runtime for SDK Core."""

    @staticmethod
    def _raise_in_thread(thread_id: int, exc_type: Type[BaseException]) -> bool:
        """Internal helper for raising an exception in thread."""
        return temporalio.bridge.temporal_sdk_bridge.raise_in_thread(
            thread_id, exc_type
        )

    def __init__(self, *, telemetry: TelemetryConfig) -> None:
        """Create SDK Core runtime."""
        self._ref = temporalio.bridge.temporal_sdk_bridge.init_runtime(telemetry)


@dataclass(frozen=True)
class TracingConfig:
    """Python representation of the Rust struct for tracing config."""

    filter: str
    opentelemetry: OpenTelemetryConfig


@dataclass(frozen=True)
class LoggingConfig:
    """Python representation of the Rust struct for logging config."""

    filter: str
    forward: bool


@dataclass(frozen=True)
class MetricsConfig:
    """Python representation of the Rust struct for metrics config."""

    opentelemetry: Optional[OpenTelemetryConfig]
    prometheus: Optional[PrometheusConfig]


@dataclass(frozen=True)
class OpenTelemetryConfig:
    """Python representation of the Rust struct for OpenTelemetry config."""

    url: str
    headers: Mapping[str, str]
    metric_periodicity_millis: Optional[int]


@dataclass(frozen=True)
class PrometheusConfig:
    """Python representation of the Rust struct for Prometheus config."""

    bind_address: str


@dataclass(frozen=True)
class TelemetryConfig:
    """Python representation of the Rust struct for telemetry config."""

    tracing: Optional[TracingConfig]
    logging: Optional[LoggingConfig]
    metrics: Optional[MetricsConfig]
