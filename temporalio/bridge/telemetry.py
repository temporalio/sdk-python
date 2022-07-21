"""Telemetry for SDK Core."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Mapping, Optional

import temporalio.bridge.temporal_sdk_bridge


@dataclass
class TelemetryConfig:
    """Python representation of the Rust struct for configuring telemetry."""

    tracing_filter: Optional[str] = "temporal_sdk_core=WARN"
    otel_tracing: Optional[OtelCollectorConfig] = None
    log_console: bool = True
    log_forwarding_level: Optional[str] = None
    otel_metrics: Optional[OtelCollectorConfig] = None
    prometheus_metrics: Optional[PrometheusMetricsConfig] = None


@dataclass
class OtelCollectorConfig:
    """Python representation of the Rust struct for configuring OTel."""

    url: str
    headers: Mapping[str, str]


@dataclass
class PrometheusMetricsConfig:
    """Python representation of the Rust struct for configuring Prometheus."""

    bind_address: str


_inited = False


def init_telemetry(
    config: TelemetryConfig, *, warn_if_already_inited: bool = True
) -> bool:
    """Initialize telemetry with the given configuration.

    Does nothing if already called.
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
