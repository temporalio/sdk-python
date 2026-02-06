"""OpenTelemetry integration for OpenAI Agents in Temporal workflows.

This module provides utilities for properly exporting OpenAI agent telemetry
to OpenTelemetry endpoints from within Temporal workflows, handling workflow
replay semantics correctly.
"""

from temporalio.contrib.opentelemetry._generator import TemporalIdGenerator
from temporalio.contrib.opentelemetry._instrumentation import (
    with_instrumentation_context,
)
from temporalio.contrib.opentelemetry._interceptors import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._processor import TemporalSpanProcessor

__all__ = [
    "TemporalSpanProcessor",
    "TemporalIdGenerator",
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "with_instrumentation_context",
]
