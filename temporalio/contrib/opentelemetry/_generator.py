from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import INVALID_SPAN_ID, INVALID_TRACE_ID

from temporalio import workflow


class TemporalIdGenerator(IdGenerator):
    """OpenTelemetry ID generator that provides deterministic IDs for Temporal workflows.

    This generator ensures that span and trace IDs are deterministic when running
    within Temporal workflows by using the workflow's deterministic random source.
    This is crucial for maintaining consistency across workflow replays.
    """

    def __init__(self):
        """Initialize the ID generator with empty trace and span pools."""
        self.traces = []
        self.spans = []

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
            return self.spans.pop()

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
            return self.traces.pop()

        trace_id = get_rand_bits(128)
        while trace_id == INVALID_TRACE_ID:
            trace_id = get_rand_bits(128)
        return trace_id