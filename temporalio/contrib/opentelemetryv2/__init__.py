"""OpenTelemetry v2 integration for Temporal SDK.

This package provides OpenTelemetry tracing integration for Temporal workflows,
activities, and other operations. It includes automatic span creation and
propagation for distributed tracing.
"""

from temporalio.contrib.opentelemetryv2._interceptor import TracingInterceptor
from temporalio.contrib.opentelemetryv2._plugin import OpenTelemetryPlugin

__all__ = ["TracingInterceptor", "OpenTelemetryPlugin"]
