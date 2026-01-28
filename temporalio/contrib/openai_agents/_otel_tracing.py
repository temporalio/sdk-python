"""OpenTelemetry tracing support for OpenAI Agents in Temporal workflows."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span

from temporalio import workflow

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider


def setup_tracing(tracer_provider: TracerProvider) -> None:
    """Set up OpenAI Agents OTEL tracing with OpenInference instrumentation.

    This instruments the OpenAI Agents SDK with OpenInference, which converts
    agent spans to OTEL spans. Combined with opentelemetry passthrough in the
    sandbox, this enables proper span parenting inside Temporal's workflows.

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
        Consider using ReplayFilteringSpanProcessor instead for automatic
        filtering of all spans during replay.

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
