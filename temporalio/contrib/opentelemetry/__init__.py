"""OpenTelemetry integration for Temporal.

This module provides OpenTelemetry tracing support for Temporal workflows
and activities. It includes:

- :py:class:`TracingInterceptor`: Interceptor for creating and propagating
  OpenTelemetry spans across client, worker, and workflow boundaries.

- :py:class:`OtelTracingPlugin`: A plugin that configures TracingInterceptor
  with context-only propagation (no spans) and automatically configures
  sandbox passthrough for opentelemetry.

Basic usage with TracingInterceptor (creates Temporal spans):

    from temporalio.contrib.opentelemetry import TracingInterceptor

    client = await Client.connect(
        "localhost:7233",
        interceptors=[TracingInterceptor()],
    )

Usage with OtelTracingPlugin (context propagation only, for use with other
instrumentation like OpenInference):

    from temporalio.contrib.opentelemetry import OtelTracingPlugin

    plugin = OtelTracingPlugin(tracer_provider=tracer_provider)

    client = await Client.connect(
        "localhost:7233",
        plugins=[plugin],
    )

    # Sandbox passthrough is configured automatically by the plugin
    worker = Worker(
        client,
        task_queue="my-queue",
        workflows=[MyWorkflow],
    )

The plugin automatically:
- Configures opentelemetry as a sandbox passthrough module
- Wraps tracer provider span processors with replay filtering

Design Notes - Cross-Process Trace Propagation:

    OpenTelemetry spans are process-local by design. The OTEL specification
    states: "Spans are not meant to be used to propagate information within
    a process." Only SpanContext (trace_id, span_id, trace_flags) crosses
    process boundaries via propagators.

    This is intentional - Span objects contain mutable state, thread locks,
    and processor references that cannot be serialized. SpanContext is an
    immutable tuple designed for cross-process propagation.

    For Temporal workflows that may execute across multiple workers:

    - TracingInterceptor serializes SpanContext (not Span) into headers
    - Remote workers deserialize SpanContext and wrap it in NonRecordingSpan
    - Child spans created in remote workers link to the parent via span_id
    - No span is ever "opened" in one process and "closed" in another

    With create_spans=True, workflow spans are created and immediately ended
    (same start/end timestamp) to avoid cross-process lifecycle issues.

    With create_spans=False (OtelTracingPlugin default), no Temporal spans
    are created - only context is propagated for other instrumentation.
"""

from ._id_generator import TemporalIdGenerator
from ._otel_tracing_plugin import OtelTracingPlugin
from ._tracing_interceptor import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
    default_text_map_propagator,
    workflow,
)

__all__ = [
    "OtelTracingPlugin",
    "TemporalIdGenerator",
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "default_text_map_propagator",
    "workflow",
]
