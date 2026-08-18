"""OpenTelemetry v2 integration for Temporal SDK.

This package provides OpenTelemetry tracing and metrics integration for
Temporal workflows, activities, and other operations. It includes automatic
span creation and propagation for distributed tracing, and a
:py:class:`MetricsExporter` for exporting Temporal SDK/Core metrics through
the standard OpenTelemetry metrics API.
"""

from temporalio.contrib.opentelemetry._interceptor import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._metrics_exporter import MetricsExporter
from temporalio.contrib.opentelemetry._otel_interceptor import OpenTelemetryInterceptor
from temporalio.contrib.opentelemetry._plugin import OpenTelemetryPlugin
from temporalio.contrib.opentelemetry._tracer_provider import create_tracer_provider

__all__ = [
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "OpenTelemetryInterceptor",
    "OpenTelemetryPlugin",
    "MetricsExporter",
    "create_tracer_provider",
]
