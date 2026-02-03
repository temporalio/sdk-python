from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
)

import temporalio.workflow


class TemporalIdGenerator(RandomIdGenerator):
    """OpenTelemetry ID generator that uses Temporal's deterministic random generator.

    This generator uses Temporal's workflow-safe random number generator when
    inside a workflow execution, ensuring deterministic span and trace IDs
    across workflow replays. Falls back to standard random generation outside
    of workflows.
    """

    def generate_span_id(self) -> int:
        """Generate a span ID using Temporal's deterministic random when in workflow.

        Returns:
            A 64-bit span ID.
        """
        if (
            temporalio.workflow.in_workflow()
            and not temporalio.workflow.unsafe.is_read_only()
        ):
            span_id = temporalio.workflow.random().getrandbits(64)
            while span_id == INVALID_SPAN_ID:
                span_id = temporalio.workflow.random().getrandbits(64)
            return span_id
        else:
            return super().generate_span_id()

    def generate_trace_id(self) -> int:
        """Generate a trace ID using Temporal's deterministic random when in workflow.

        Returns:
            A 128-bit trace ID.
        """
        if (
            temporalio.workflow.in_workflow()
            and not temporalio.workflow.unsafe.is_read_only()
        ):
            trace_id = temporalio.workflow.random().getrandbits(128)
            while trace_id == INVALID_TRACE_ID:
                trace_id = temporalio.workflow.random().getrandbits(128)
            return trace_id
        else:
            return super().generate_trace_id()
