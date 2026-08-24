"""Datadog tracing integration for the Temporal Python SDK.

This package provides a Datadog (``ddtrace``) tracing interceptor for the
Temporal Python SDK.

Usage::

    from ddtrace import patch
    from temporalio.client import Client
    from temporalio.contrib.datadog import DatadogTracingInterceptor

    patch(logging=True)  # opt in to dd.trace_id log injection

    interceptor = DatadogTracingInterceptor(
        service_name="my-service",
        extra_tags={"deployment.environment": "prod"},
    )
    client = await Client.connect("localhost:7233", interceptors=[interceptor])
"""

from temporalio.contrib.datadog._interceptor import DatadogTracingInterceptor
from temporalio.contrib.datadog._workflow_interceptor import (
    disconnect_trace_span_from_workflow_context,
    span_from_workflow_context,
)
from temporalio.contrib.datadog._wrapped_tracer import (
    FinishContext,
    FinishResult,
)

__all__ = [
    "DatadogTracingInterceptor",
    "FinishContext",
    "FinishResult",
    "disconnect_trace_span_from_workflow_context",
    "span_from_workflow_context",
]
