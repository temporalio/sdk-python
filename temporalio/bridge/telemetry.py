"""Telemetry for SDK Core."""

from dataclasses import dataclass
from typing import Optional

import temporal_sdk_bridge


@dataclass
class TelemetryConfig:
    """Python representation of the Rust struct for configuring telemetry."""

    otel_collector_url: Optional[str] = None
    tracing_filter: Optional[str] = None
    log_forwarding_level: Optional[str] = None
    prometheus_export_bind_address: Optional[str] = None


_inited = False


def init_telemetry(config: TelemetryConfig) -> bool:
    """Initialize telemetry with the given configuration.

    Does nothing if already called.
    """
    global _inited
    if _inited:
        return False
    temporal_sdk_bridge.init_telemetry(config)
    _inited = True
    return True
