"""OTEL-aware variant of OpenAI Agents trace interceptor."""

from __future__ import annotations

from typing import Any

import opentelemetry.trace
from opentelemetry.context import attach
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)

from ._trace_interceptor import (
    OpenAIAgentsContextPropagationInterceptor,
    _InputWithHeaders,
)


class OTelOpenAIAgentsContextPropagationInterceptor(
    OpenAIAgentsContextPropagationInterceptor
):
    """OTEL-aware variant that enhances headers with OpenTelemetry span context."""

    def header_contents(self) -> dict[str, Any]:
        """Get header contents enhanced with OpenTelemetry span context.

        Returns:
            Dictionary containing trace context with OTEL span information.
        """
        otel_span = opentelemetry.trace.get_current_span()

        if otel_span and otel_span.get_span_context().is_valid:
            span_context = otel_span.get_span_context()
            return {
                **super().header_contents(),
                "otelSpanId": span_context.span_id,
                "otelTraceId": span_context.trace_id,
            }
        else:
            return super().header_contents()

    def context_from_header(
        self,
        input: _InputWithHeaders,
    ):
        """Extracts and initializes trace information the input header."""
        span_info = self.get_header_contents(input)

        if span_info is None:
            return
        otel_span_id = span_info.get("otelSpanId")
        otel_trace_id = span_info.get("otelTraceId")

        # Parent OTEL spans started here to the caller's span. The Agents SDK trace
        # and span restored below are not started, so OpenInference never registers
        # copies of them under the caller's IDs.
        if otel_span_id and otel_trace_id:
            attach(
                set_span_in_context(
                    NonRecordingSpan(
                        SpanContext(
                            trace_id=otel_trace_id,
                            span_id=otel_span_id,
                            is_remote=True,
                            trace_flags=TraceFlags(TraceFlags.SAMPLED),
                        )
                    )
                )
            )

        self.trace_context_from_header_contents(span_info)
        self.span_context_from_header_contents(span_info)
