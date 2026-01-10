"""OpenTelemetry tracing plugin for OpenAI Agents in Temporal workflows."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from opentelemetry import trace as otel_trace

from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.plugin import SimplePlugin

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider


def setup_tracing(tracer_provider: TracerProvider) -> None:
    """Set up OpenAI Agents OTEL tracing with Temporal sandbox support.

    This instruments the OpenAI Agents SDK with OpenInference, which converts
    agent spans to OTEL spans. Combined with TemporalAwareContext, this enables
    proper span parenting inside Temporal's sandboxed workflows.

    Args:
        tracer_provider: The TracerProvider to use for creating spans.
    """
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

    otel_trace.set_tracer_provider(tracer_provider)
    OpenAIAgentsInstrumentor().instrument(tracer_provider=tracer_provider)


class OtelTracingPlugin(SimplePlugin):
    """Plugin for OTEL context propagation in Temporal workflows with OpenAI Agents.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin enables clean OpenTelemetry tracing through Temporal workflows:

    1. Sets OTEL_PYTHON_CONTEXT to use TemporalAwareContext (survives sandbox)
    2. Uses TracingInterceptor with create_spans=False (context only, no Temporal spans)
    3. Optionally instruments OpenAI Agents SDK with OpenInference

    The result is traces with only your application spans, proper parent-child
    relationships, and no Temporal infrastructure spans.

    Args:
        tracer_provider: The TracerProvider for creating spans. If provided,
            setup_tracing() is called automatically to instrument OpenAI Agents.

    Example:
        >>> from opentelemetry.sdk.trace import TracerProvider
        >>> from temporalio.contrib.openai_agents import OpenAIAgentsPlugin, OtelTracingPlugin
        >>>
        >>> tracer_provider = TracerProvider()
        >>> # ... configure tracer_provider with exporters ...
        >>>
        >>> openai_plugin = OpenAIAgentsPlugin(create_spans=False)
        >>> otel_plugin = OtelTracingPlugin(tracer_provider=tracer_provider)
        >>>
        >>> client = await Client.connect("localhost:7233", plugins=[openai_plugin, otel_plugin])
    """

    def __init__(self, tracer_provider: TracerProvider | None = None) -> None:
        """Initialize the OTEL tracing plugin.

        Args:
            tracer_provider: The TracerProvider for creating spans. If provided,
                setup_tracing() is called automatically to instrument OpenAI Agents.
        """
        # Ensure TemporalAwareContext is used for OTEL context
        # This makes get_current_span() work inside Temporal's sandbox
        if "OTEL_PYTHON_CONTEXT" not in os.environ:
            os.environ["OTEL_PYTHON_CONTEXT"] = "temporal_aware_context"

        # Set up tracing if provider given
        if tracer_provider is not None:
            setup_tracing(tracer_provider)

        # Use TracingInterceptor with create_spans=False
        # This propagates OTEL context via W3C TraceContext headers
        # but doesn't create any Temporal spans
        interceptor = TracingInterceptor(create_spans=False)

        super().__init__(
            name="OtelTracingPlugin",
            worker_interceptors=[interceptor],
            client_interceptors=[interceptor],
        )
