"""OpenTelemetry integration for OpenAI Agents in Temporal workflows.

This module provides utilities for properly exporting OpenAI agent telemetry
to OpenTelemetry endpoints from within Temporal workflows, handling workflow
replay semantics correctly.
"""

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import INVALID_SPAN_ID, INVALID_TRACE_ID

from temporalio import workflow


class TemporalIdGenerator(IdGenerator):
    """OpenTelemetry ID generator that provides deterministic IDs for Temporal workflows.

    This generator ensures that span and trace IDs are deterministic when running
    within Temporal workflows by using the workflow's deterministic random source.
    This is crucial for maintaining consistency across workflow replays.

    Can be seeded with OpenTelemetry span IDs from client context to maintain
    proper span parenting across the client-workflow boundary.
    """

    def __init__(self):
        """Initialize the ID generator with empty trace and span pools."""
        self.traces = []
        self.spans = []

    def seed_span_id(self, span_id: int) -> None:
        """Seed the generator with a span ID to use as the first result.

        This is typically used to maintain OpenTelemetry span parenting
        when crossing the client-workflow boundary.

        Args:
            span_id: The span ID to use as the first generated span ID.
        """
        # Insert at the beginning so it's used as the first span ID
        self.spans.insert(0, span_id)

    def seed_trace_id(self, trace_id: int) -> None:
        """Seed the generator with a trace ID to use as the first result.

        Args:
            trace_id: The trace ID to use as the first generated trace ID.
        """
        # Insert at the beginning so it's used as the first trace ID
        self.traces.insert(0, trace_id)

    def generate_span_id(self) -> int:
        """Generate a deterministic span ID.

        Uses the workflow's deterministic random source when in a workflow context,
        otherwise falls back to system random.

        Returns:
            A 64-bit span ID that is guaranteed not to be INVALID_SPAN_ID.
        """
        if workflow.in_workflow():
            get_rand_bits = workflow.random().getrandbits
        else:
            import random

            get_rand_bits = random.getrandbits

        if len(self.spans) > 0:
            return self.spans.pop(0)  # Use FIFO to get seeded spans first

        span_id = get_rand_bits(64)
        while span_id == INVALID_SPAN_ID:
            span_id = get_rand_bits(64)
        return span_id

    def generate_trace_id(self) -> int:
        """Generate a deterministic trace ID.

        Uses the workflow's deterministic random source when in a workflow context,
        otherwise falls back to system random.

        Returns:
            A 128-bit trace ID that is guaranteed not to be INVALID_TRACE_ID.
        """
        if workflow.in_workflow():
            get_rand_bits = workflow.random().getrandbits
        else:
            import random

            get_rand_bits = random.getrandbits
        if len(self.traces) > 0:
            return self.traces.pop(0)  # Use FIFO to get seeded traces first

        trace_id = get_rand_bits(128)
        while trace_id == INVALID_TRACE_ID:
            trace_id = get_rand_bits(128)
        return trace_id


class TemporalSpanProcessor(SimpleSpanProcessor):
    """A span processor that handles Temporal workflow replay semantics.

    This processor ensures that spans are only exported when workflows actually
    complete, not during intermediate replays. This is crucial for maintaining
    correct telemetry data when using OpenAI agents within Temporal workflows.

    Example usage:
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from temporalio.contrib.openai_agents._temporal_trace_provider import TemporalIdGenerator
        from temporalio.contrib.openai_agents._otel import TemporalSpanProcessor
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        exporter = InMemorySpanExporter()
        provider = trace_sdk.TracerProvider(id_generator=TemporalIdGenerator())
        provider.add_span_processor(TemporalSpanProcessor(exporter))
        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)
    """

    def on_end(self, span: ReadableSpan) -> None:
        """Handle span end events, skipping export during workflow replay.

        Args:
            span: The span that has ended.
        """
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            # Skip exporting spans during workflow replay to avoid duplicate telemetry
            return
        super().on_end(span)
