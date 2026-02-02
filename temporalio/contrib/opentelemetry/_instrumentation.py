from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, Callable

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter


class Instrumentor(ABC):
    @abstractmethod
    def instrument(self, **kwargs: Any):
        """Instrument the library"""

    @abstractmethod
    def uninstrument(self, **kwargs: Any):
        """Uninstrument the library"""

@asynccontextmanager
async def with_instrumentation_context(span_exporters: list[SpanExporter], instrumentor: Instrumentor):
    from temporalio.contrib.opentelemetry import (
        TemporalIdGenerator,
        TemporalSpanProcessor,
    )
    from opentelemetry import trace
    from opentelemetry.sdk import trace as trace_sdk

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