"""OpenTelemetry workflow utilities for Temporal SDK.

This module provides workflow-safe OpenTelemetry span creation and context
management utilities for use within Temporal workflows. All functions in
this module are designed to work correctly during workflow replay.
"""

from __future__ import annotations

import warnings

import opentelemetry.util.types
from opentelemetry.trace import (
    Tracer,
)

import temporalio.workflow
from temporalio.contrib.opentelemetry import TracingWorkflowInboundInterceptor
from temporalio.exceptions import ApplicationError


def _try_get_tracer() -> Tracer | None:
    tracer = getattr(
        temporalio.workflow.instance(), "__temporal_opentelemetry_tracer", None
    )
    if tracer is not None and not isinstance(tracer, Tracer):
        raise ApplicationError(
            "Failed to get temporal OpenTelemetry tracer from workflow. It was present but not a Tracer. This is unexpected."
        )
    return tracer


def tracer():
    """Get an OpenTelemetry Tracer which functions inside a Temporal workflow.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.
    """
    tracer = _try_get_tracer()
    if tracer is None or not isinstance(tracer, Tracer):
        raise ApplicationError(
            "Failed to get temporal OpenTelemetry tracer from workflow. You may not have registered the OpenTelemetryPlugin."
        )

    return tracer


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

    To create a long-running span or to create a span that actually spans other code use OpenTelemetryPlugin and tracer().

    Args:
        name: Name of the span.
        attributes: Attributes to set on the span if any. Workflow ID and
            run ID are automatically added.
        exception: Optional exception to record on the span.
    """
    # Check for v2 Tracer first
    if tracer := _try_get_tracer():
        warnings.warn(
            "When using OpenTelemetryPlugin, you should prefer workflow.tracer().",
            DeprecationWarning,
        )
        span = tracer.start_span(name, attributes=attributes)
        if exception:
            span.record_exception(exception)
        span.end()
        return

    interceptor = TracingWorkflowInboundInterceptor._from_context()
    if interceptor:
        interceptor._completed_span(
            name, additional_attributes=attributes, exception=exception
        )
