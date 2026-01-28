"""Deterministic ID generator for Temporal workflows."""

from __future__ import annotations

from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import INVALID_SPAN_ID, INVALID_TRACE_ID


class TemporalIdGenerator(IdGenerator):
    """IdGenerator that produces deterministic IDs in workflow context.

    Uses workflow.random() which is seeded deterministically by Temporal and
    replays identically. Outside workflow context (activities, client code),
    falls back to standard random generation.

    This enables real-duration spans in workflows:
    - First execution: Span with ID X created, exported with real duration
    - Replay: Same ID X generated (deterministic), span filtered by
      ReplayFilteringSpanProcessor
    - Parent-child relationships remain stable across executions

    Usage:
        from opentelemetry.sdk.trace import TracerProvider
        from temporalio.contrib.opentelemetry import TemporalIdGenerator

        tracer_provider = TracerProvider(id_generator=TemporalIdGenerator())

    Or via OtelTracingPlugin:
        plugin = OtelTracingPlugin(
            tracer_provider=tracer_provider,
            deterministic_ids=True,
        )
    """

    def __init__(self) -> None:
        """Initialize the ID generator with a fallback for non-workflow contexts."""
        self._fallback = RandomIdGenerator()

    def generate_span_id(self) -> int:
        """Generate a span ID.

        In workflow context, uses deterministic RNG. Otherwise, uses random.
        """
        if self._in_workflow_context():
            from temporalio import workflow

            span_id = workflow.random().getrandbits(64)
            while span_id == INVALID_SPAN_ID:
                span_id = workflow.random().getrandbits(64)
            return span_id
        return self._fallback.generate_span_id()

    def generate_trace_id(self) -> int:
        """Generate a trace ID.

        In workflow context, uses deterministic RNG. Otherwise, uses random.
        """
        if self._in_workflow_context():
            from temporalio import workflow

            trace_id = workflow.random().getrandbits(128)
            while trace_id == INVALID_TRACE_ID:
                trace_id = workflow.random().getrandbits(128)
            return trace_id
        return self._fallback.generate_trace_id()

    def _in_workflow_context(self) -> bool:
        """Check if we're in workflow context where random() is available."""
        from temporalio import workflow

        try:
            # workflow.in_workflow() returns True if in workflow context
            if not workflow.in_workflow():
                return False
            # Also check we're not in a read-only context (query handler)
            # by actually calling random() - it raises ReadOnlyContextError if not allowed
            workflow.random()
            return True
        except Exception:
            return False
