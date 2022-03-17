"""Telemetry for SDK Core."""

import warnings
from dataclasses import dataclass
from typing import Optional

import temporalio.bridge.temporal_sdk_bridge


@dataclass
class TelemetryConfig:
    """Python representation of the Rust struct for configuring telemetry."""

    otel_collector_url: Optional[str] = None
    tracing_filter: Optional[str] = None
    log_forwarding_level: Optional[str] = None
    prometheus_export_bind_address: Optional[str] = None


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
