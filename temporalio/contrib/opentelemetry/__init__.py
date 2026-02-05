"""OpenTelemetry v2 integration for Temporal SDK.

This package provides OpenTelemetry tracing integration for Temporal workflows,
activities, and other operations. It includes automatic span creation and
propagation for distributed tracing.
"""

from temporalio.contrib.opentelemetry._interceptor import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._interceptor_v2 import TracingInterceptorV2
from temporalio.contrib.opentelemetry._plugin import OpenTelemetryPlugin

__all__ = [
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "TracingInterceptorV2",
    "OpenTelemetryPlugin",
]
