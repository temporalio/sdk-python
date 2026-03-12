"""OTEL-aware variant of OpenAI Agents trace interceptor."""

from __future__ import annotations

from typing import Any

import opentelemetry.trace

import temporalio.converter

from ..opentelemetry._id_generator import TemporalIdGenerator
from ._trace_interceptor import (
    OpenAIAgentsContextPropagationInterceptor,
    _InputWithHeaders,
)


class OTelOpenAIAgentsContextPropagationInterceptor(
    OpenAIAgentsContextPropagationInterceptor
):
    """OTEL-aware variant that enhances headers with OpenTelemetry span context."""

    def __init__(
        self,
        otel_id_generator: TemporalIdGenerator,
        payload_converter: temporalio.converter.PayloadConverter = temporalio.converter.default().payload_converter,
        add_temporal_spans: bool = True,
    ) -> None:
        """Initialize OTEL-aware context propagation interceptor.

        Args:
            otel_id_generator: Generator for OTEL-compatible IDs.
            payload_converter: Converter for serializing trace context.
            add_temporal_spans: Whether to add Temporal-specific spans.
        """
        super().__init__(payload_converter, add_temporal_spans, start_traces=True)
        self._otel_id_generator = otel_id_generator

    def header_contents(self) -> dict[str, Any]:
        """Get header contents enhanced with OpenTelemetry span context.

        Returns:
            Dictionary containing trace context with OTEL span information.
        """
        otel_span = opentelemetry.trace.get_current_span()

        if otel_span and otel_span.get_span_context().is_valid:
            otel_span_id = otel_span.get_span_context().span_id
            return {
                **super().header_contents(),
                "otelSpanId": otel_span_id,
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

        # If only a trace was propagated from the caller, we need to seed for trace context
        if otel_span_id and self._otel_id_generator and span_info.get("spanId") is None:
            self._otel_id_generator.seed_span_id(otel_span_id)

        super().trace_context_from_header_contents(span_info)

        # If a span was propagated from the caller, we need to seed for span context
        if (
            otel_span_id
            and self._otel_id_generator
            and span_info.get("spanId") is not None
        ):
            self._otel_id_generator.seed_span_id(otel_span_id)

        super().span_context_from_header_contents(span_info)
