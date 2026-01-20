"""OpenTelemetry integration for OpenAI Agents in Temporal workflows.

This module provides utilities for properly exporting OpenAI agent telemetry
to OpenTelemetry endpoints from within Temporal workflows, handling workflow
replay semantics correctly.
"""

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from temporalio import workflow


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