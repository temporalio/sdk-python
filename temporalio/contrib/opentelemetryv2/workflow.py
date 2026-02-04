"""OpenTelemetry workflow utilities for Temporal SDK.

This module provides workflow-safe OpenTelemetry span creation and context
management utilities for use within Temporal workflows. All functions in
this module are designed to work correctly during workflow replay.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from opentelemetry.context import Context
from opentelemetry.trace import (
    Link,
    SpanKind,
    Tracer,
)
from opentelemetry.trace.span import Span
from opentelemetry.util import types

import temporalio.workflow
from temporalio.exceptions import ApplicationError


def _get_tracer():
    tracer = getattr(temporalio.workflow.instance(), "__temporal_opentelemetry_tracer")
    if tracer is None or not isinstance(tracer, Tracer):
        raise ApplicationError(
            "Failed to get temporal opentelemetry tracer from workflow. You may not have registered the OpenTelemetryPlugin."
        )

    return tracer


@contextmanager
def start_as_current_span(
    name: str,
    context: Context | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: types.Attributes = None,
    links: Sequence[Link] | None = None,
    record_exception: bool = True,
    set_status_on_exception: bool = True,
    end_on_exit: bool = True,
) -> Iterator[Span]:
    """Start a new OpenTelemetry span as current span within a Temporal workflow.

    This function creates a new span using Temporal's workflow-safe time source
    to ensure deterministic span timing across workflow replays.

    Args:
        name: The span name.
        context: Optional OpenTelemetry context to use as parent.
        kind: The span kind (default: SpanKind.INTERNAL).
        attributes: Optional span attributes.
        links: Optional span links.
        record_exception: Whether to record exceptions as span events.
        set_status_on_exception: Whether to set span status on exception.
        end_on_exit: Whether to end the span when exiting context.

    Yields:
        The created span.

    Raises:
        ApplicationError: If unable to get the tracer from workflow context.
    """
    tracer = _get_tracer()
    with tracer.start_as_current_span(
        name,
        start_time=temporalio.workflow.time_ns(),
        context=context,
        kind=kind,
        attributes=attributes,
        links=links,
        record_exception=record_exception,
        set_status_on_exception=set_status_on_exception,
        end_on_exit=end_on_exit,
    ) as span:
        yield span
