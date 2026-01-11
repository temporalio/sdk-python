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
"""

from ._otel_tracing_plugin import OtelTracingPlugin
from ._tracing_interceptor import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
    default_text_map_propagator,
    workflow,
)

__all__ = [
    "TracingInterceptor",
    "TracingWorkflowInboundInterceptor",
    "default_text_map_propagator",
    "workflow",
    "OtelTracingPlugin",
]
