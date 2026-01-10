"""OpenTelemetry integration for Temporal.

This module provides OpenTelemetry tracing support for Temporal workflows
and activities. It includes:

- :py:class:`TracingInterceptor`: Interceptor for creating and propagating
  OpenTelemetry spans across client, worker, and workflow boundaries.

- :py:class:`OtelTracingPlugin`: A plugin that configures TracingInterceptor
  with context-only propagation (no spans) and provides sandbox passthrough
  configuration.

- :py:class:`ReplayFilteringSpanProcessor`: A span processor wrapper that
  filters out spans created during workflow replay.

Basic usage with TracingInterceptor (creates Temporal spans):

    from temporalio.contrib.opentelemetry import TracingInterceptor

    client = await Client.connect(
        "localhost:7233",
        interceptors=[TracingInterceptor()],
    )

Usage with OtelTracingPlugin (context propagation only, for use with other
instrumentation like OpenInference):

    from temporalio.contrib.opentelemetry import OtelTracingPlugin
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    plugin = OtelTracingPlugin(tracer_provider=tracer_provider)

    client = await Client.connect(
        "localhost:7233",
        plugins=[plugin],
    )

    worker = Worker(
        client,
        task_queue="my-queue",
        workflows=[MyWorkflow],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=plugin.sandbox_restrictions
        ),
    )

Sandbox Context Propagation:

    For OTEL context to propagate correctly inside workflow sandboxes,
    the opentelemetry module must be in the sandbox passthrough list.
    This ensures the same ContextVar instance is used inside and outside
    the sandbox, allowing get_current_span() to work correctly.

    The OtelTracingPlugin provides a sandbox_restrictions property that
    includes opentelemetry in the passthrough modules. Alternatively,
    you can configure this manually:

        from temporalio.worker.workflow_sandbox import (
            SandboxedWorkflowRunner,
            SandboxRestrictions,
        )

        worker = Worker(
            client,
            workflows=[...],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "opentelemetry"
                )
            ),
        )
"""

from ._otel_tracing_plugin import OtelTracingPlugin
from ._replay_filtering_processor import ReplayFilteringSpanProcessor
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
    "ReplayFilteringSpanProcessor",
]
