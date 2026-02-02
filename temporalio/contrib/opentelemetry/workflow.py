import opentelemetry.util.types
from temporalio.contrib.opentelemetry._interceptors import TracingWorkflowInboundInterceptor


def completed_span(
    name: str,
    *,
    attributes: opentelemetry.util.types.Attributes = None,
    exception: Exception | None = None,
) -> None:
    """Create and end an OpenTelemetry span.

    Note, this will only create and record when the workflow is not
    replaying and if there is a current span (meaning the client started a
    span and this interceptor is configured on the worker and the span is on
    the context).

    There is currently no way to create a long-running span or to create a
    span that actually spans other code.

    Args:
        name: Name of the span.
        attributes: Attributes to set on the span if any. Workflow ID and
            run ID are automatically added.
        exception: Optional exception to record on the span.
    """
    interceptor = TracingWorkflowInboundInterceptor._from_context()
    if interceptor:
        interceptor._completed_span(
            name, additional_attributes=attributes, exception=exception
        )
