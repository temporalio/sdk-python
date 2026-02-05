"""OpenTelemetry workflow utilities for Temporal SDK.

This module provides workflow-safe OpenTelemetry span creation and context
management utilities for use within Temporal workflows. All functions in
this module are designed to work correctly during workflow replay.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator, Sequence

import opentelemetry.util.types
from opentelemetry.context import Context
from opentelemetry.trace import (
    Link,
    SpanKind,
    Tracer,
)
from opentelemetry.trace.span import Span
from opentelemetry.util import types
from opentelemetry.util._decorator import _agnosticcontextmanager

import temporalio.workflow
from temporalio.contrib.opentelemetry import TracingWorkflowInboundInterceptor
from temporalio.exceptions import ApplicationError


def _try_get_tracer() -> Tracer | None:
    tracer = getattr(temporalio.workflow.instance(), "__temporal_opentelemetry_tracer")
    if not isinstance(tracer, Tracer):
        raise ApplicationError(
            "Failed to get temporal OpenTelemetry tracer from workflow. It was present but not a Tracer. This is unexpected."
        )
    return tracer


def _get_tracer():
    tracer = _try_get_tracer()
    if tracer is None or not isinstance(tracer, Tracer):
        raise ApplicationError(
            "Failed to get temporal OpenTelemetry tracer from workflow. You may not have registered the OpenTelemetryPlugin."
        )

    return tracer


class _TemporalTracer(Tracer):
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: types.Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> "Span":
        return self._tracer.start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time or temporalio.workflow.time_ns(),
            record_exception,
            set_status_on_exception,
        )

    @_agnosticcontextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: types.Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator["Span"]:
        with self._tracer.start_as_current_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time or temporalio.workflow.time_ns(),
            record_exception,
            set_status_on_exception,
        ) as span:
            yield span


def tracer() -> Tracer:
    """Get an OpenTelemetry Tracer which functions inside a Temporal workflow."""
    return _TemporalTracer(_get_tracer())


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
