from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from opentelemetry.sdk.trace.export import SpanExporter


class Instrumentor(Protocol):
    """Protocol for instrumenting libraries with OpenTelemetry."""

    def instrument(self, **kwargs: Any):
        """Instrument the library"""

    def uninstrument(self, **kwargs: Any):
        """Uninstrument the library"""


@asynccontextmanager
async def with_instrumentation_context(
    span_exporters: Sequence[SpanExporter], instrumentor: Instrumentor
):
    """Context manager that sets up OpenTelemetry instrumentation with Temporal-specific components.

    Args:
        span_exporters: Sequence of span exporters to use for tracing
        instrumentor: Instrumentor instance to use for library instrumentation
    """
    from opentelemetry import trace
    from opentelemetry.sdk import trace as trace_sdk

    from temporalio.contrib.opentelemetry import (
        TemporalIdGenerator,
        TemporalSpanProcessor,
    )

    # Create trace provider with deterministic ID generation
    provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())

    # Switch the current tracer provider
    old_provider = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)

    # Add all exporters with TemporalSpanProcessor wrapper
    for exporter in span_exporters:
        processor = TemporalSpanProcessor(exporter)
        provider.add_span_processor(processor)

    instrumentor.instrument(tracer_provider=provider)
    try:
        yield
    finally:
        instrumentor.uninstrument()
        trace.set_tracer_provider(old_provider)
