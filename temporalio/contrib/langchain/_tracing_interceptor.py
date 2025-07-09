"""OpenTelemetry tracing interceptor for LangChain-Temporal integration."""

import json
from typing import Any, Optional

from temporalio import client, worker, converter

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)


class TemporalLangChainTracingInterceptor:
    """Interceptor for LangChain tracing context propagation.

    This interceptor ensures that OpenTelemetry tracing context is properly
    propagated from workflow to activity execution, allowing for end-to-end
    trace visibility of LangChain operations.
    """

    def __init__(self, payload_converter: Optional[converter.PayloadConverter] = None):
        """Initialize the tracing interceptor.

        Args:
            payload_converter: Optional custom payload converter. If not provided,
                              uses the default converter.
        """

        self._payload_converter = (
            payload_converter or converter.default().payload_converter
        )

    def intercept_client(
        self, next: client.OutboundInterceptor
    ) -> client.OutboundInterceptor:
        """Intercept client operations to inject tracing context."""
        return _TracingClientOutboundInterceptor(next, self._payload_converter)

    def intercept_activity(
        self, next: worker.ActivityInboundInterceptor
    ) -> worker.ActivityInboundInterceptor:
        """Intercept activity operations to extract tracing context."""
        return _TracingActivityInboundInterceptor(next)

    def workflow_interceptor_class(self, input: worker.WorkflowInterceptorClassInput):
        """Intercept workflow operations for tracing."""
        return _TracingWorkflowInboundInterceptor


class _TracingClientOutboundInterceptor(client.OutboundInterceptor):
    """Inject OpenTelemetry context into activity headers."""

    def __init__(
        self,
        next: client.OutboundInterceptor,
        payload_converter: converter.PayloadConverter,
    ):
        super().__init__(next)
        self._payload_converter = payload_converter

    async def execute_activity(self, input: client.ExecuteActivityInput) -> Any:
        """Execute activity with tracing context injection."""

        # Inject current OpenTelemetry context into headers
        current_span = trace.get_current_span()
        if current_span and current_span.get_span_context().is_valid:
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)

            # Add tracing headers to activity
            headers = dict(input.headers or {})
            headers["otel-trace-context"] = json.dumps(carrier)
            input = input._replace(headers=headers)

        return await self.next.execute_activity(input)


class _TracingActivityInboundInterceptor(worker.ActivityInboundInterceptor):
    """Extract OpenTelemetry context from activity headers and create child span."""

    def __init__(self, next: worker.ActivityInboundInterceptor):
        super().__init__(next)

    async def execute_activity(self, input: worker.ExecuteActivityInput) -> Any:
        """Execute activity with tracing context extraction."""

        # Extract OpenTelemetry context from headers
        span_context = None
        if input.headers and "otel-trace-context" in input.headers:
            try:
                carrier = json.loads(input.headers["otel-trace-context"])
                ctx = TraceContextTextMapPropagator().extract(carrier)
                span_context = trace.get_current_span(ctx).get_span_context()
            except Exception:
                pass  # Continue without tracing if extraction fails

        # Create child span for activity execution
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            f"langchain_activity_{input.activity.name}",
            context=trace.set_span_in_context(trace.NonRecordingSpan(span_context))
            if span_context
            else None,
        ) as span:
            # Add activity metadata to span
            span.set_attribute("temporal.activity.name", input.activity.name)
            span.set_attribute("temporal.activity.type", input.activity.activity_type)
            span.set_attribute("temporal.activity.namespace", input.activity.namespace)

            # Add LangChain specific attributes if this is a LangChain activity
            if input.activity.name in ["langchain_model_call", "langchain_tool_call"]:
                span.set_attribute("langchain.activity.type", input.activity.name)

            return await self.next.execute_activity(input)


class _TracingWorkflowInboundInterceptor(worker.WorkflowInboundInterceptor):
    """Create workflow spans for LangChain operations."""

    def __init__(self, next: worker.WorkflowInboundInterceptor):
        super().__init__(next)

    async def execute_workflow(self, input: worker.ExecuteWorkflowInput) -> Any:
        """Execute workflow with tracing span creation."""

        # Create root span for workflow execution
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            f"langchain_workflow_{input.workflow.name}"
        ) as span:
            span.set_attribute("temporal.workflow.name", input.workflow.name)
            span.set_attribute("temporal.workflow.type", input.workflow.workflow_type)
            span.set_attribute("temporal.workflow.namespace", input.workflow.namespace)

            # Add LangChain specific attributes
            span.set_attribute("langchain.workflow.enabled", True)

            return await self.next.execute_workflow(input)


# Convenience function for creating the interceptor
def create_langchain_tracing_interceptor(
    payload_converter: Optional[converter.PayloadConverter] = None,
) -> TemporalLangChainTracingInterceptor:
    """Create a LangChain tracing interceptor.

    Args:
        payload_converter: Optional custom payload converter

    Returns:
        A configured tracing interceptor

    Example:
        >>> from temporalio.contrib.langchain import create_langchain_tracing_interceptor
        >>> from temporalio.worker import Worker
        >>>
        >>> interceptor = create_langchain_tracing_interceptor()
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-queue",
        ...     workflows=[MyWorkflow],
        ...     activities=[...],
        ...     interceptors=[interceptor]
        ... )
    """
    return TemporalLangChainTracingInterceptor(payload_converter)
