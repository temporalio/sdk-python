"""OpenTelemetry v2 integration for Temporal SDK.

This package provides OpenTelemetry tracing integration for Temporal workflows,
activities, and other operations. It includes automatic span creation and
propagation for distributed tracing.
"""

from typing import Any

from temporalio.contrib.opentelemetry._interceptor import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
)

_meter_provider_import_error: ImportError | None = None
try:
    from temporalio.contrib.opentelemetry._meter_provider import (
        ReplaySafeMeterProvider,
    )
except ImportError as err:
    # opentelemetry-api < 1.12 has no opentelemetry.metrics module. Keep the
    # tracing integration importable and raise a clear error only when
    # ReplaySafeMeterProvider is actually accessed (see __getattr__ below).
    _meter_provider_import_error = err

from temporalio.contrib.opentelemetry._otel_interceptor import OpenTelemetryInterceptor
from temporalio.contrib.opentelemetry._plugin import OpenTelemetryPlugin
from temporalio.contrib.opentelemetry._tracer_provider import (
    ReplaySafeTracerProvider,
    create_tracer_provider,
)

__all__ = [
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "OpenTelemetryInterceptor",
    "OpenTelemetryPlugin",
    "ReplaySafeMeterProvider",
    "ReplaySafeTracerProvider",
    "create_tracer_provider",
]


def __getattr__(name: str) -> Any:
    # Only reachable for ReplaySafeMeterProvider when the guarded import above
    # failed; otherwise the module attribute exists and this is never called.
    if name == "ReplaySafeMeterProvider":
        raise ImportError(
            "ReplaySafeMeterProvider requires the OpenTelemetry metrics API "
            "(opentelemetry.metrics), which the installed opentelemetry-api "
            "version does not provide. Install opentelemetry-api >= 1.12 "
            "(>= 1.23 for synchronous gauge support)."
        ) from _meter_provider_import_error
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
