"""OpenTelemetry tracing plugin for OpenAI Agents in Temporal workflows."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span

from temporalio import workflow
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


@contextmanager
def workflow_span(name: str, **attributes: str) -> Iterator[Span | None]:
    """Create an OTEL span in workflow code that is replay-safe.

    This context manager creates a span only on the first execution of the
    workflow task, not during replay. This prevents span duplication when
    workflow code is re-executed during replay (e.g., when max_cached_workflows=0).

    .. warning::
        This API is experimental and may change in future versions.

    Args:
        name: The name of the span.
        **attributes: Optional attributes to set on the span.

    Yields:
        The span on first execution, None during replay.

    Example:
        >>> @workflow.defn
        ... class MyWorkflow:
        ...     @workflow.run
        ...     async def run(self) -> str:
        ...         with workflow_span("my_operation", query="test") as span:
        ...             result = await workflow.execute_activity(...)
        ...         return result

    Note:
        Spans created in activities do not need this wrapper since activities
        are not replayed - they only execute once and their results are cached.
    """
    if workflow.unsafe.is_replaying():
        yield None
    else:
        tracer = otel_trace.get_tracer(__name__)
        with tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            yield span


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
