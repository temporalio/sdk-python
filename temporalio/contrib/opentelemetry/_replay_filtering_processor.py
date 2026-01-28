"""Span processor that filters out spans created during workflow replay."""

from __future__ import annotations

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor


class ReplayFilteringSpanProcessor(SpanProcessor):
    """Wraps a SpanProcessor to filter out spans created during workflow replay.

    During Temporal workflow replay, workflow code re-executes but activities
    return cached results. Without filtering, this causes duplicate spans.
    This processor marks spans created during replay and drops them on export.

    Usage:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from temporalio.contrib.opentelemetry import ReplayFilteringSpanProcessor

        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(
            ReplayFilteringSpanProcessor(
                SimpleSpanProcessor(OTLPSpanExporter())
            )
        )
    """

    # Attribute name used to mark spans created during replay
    _REPLAY_MARKER = "_temporal_replay"

    def __init__(self, delegate: SpanProcessor) -> None:
        """Initialize the replay filtering processor.

        Args:
            delegate: The underlying span processor to delegate to for
                non-replay spans.
        """
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """Called when a span is started.

        Checks if we're currently replaying and marks the span if so.
        """
        try:
            from temporalio import workflow

            if workflow.unsafe.is_replaying():
                # Mark this span as created during replay
                setattr(span, self._REPLAY_MARKER, True)
        except Exception:
            # Not in workflow context, or workflow module not available
            # This is fine - just means we're not in a workflow
            pass

        self._delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Called when a span is ended.

        Drops spans that were marked as created during replay.
        """
        if not getattr(span, self._REPLAY_MARKER, False):
            self._delegate.on_end(span)

    def shutdown(self) -> None:
        """Shuts down the processor."""
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Forces a flush of all pending spans."""
        return self._delegate.force_flush(timeout_millis)
