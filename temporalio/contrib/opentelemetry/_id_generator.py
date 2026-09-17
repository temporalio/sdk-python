import random

from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import (
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
)

import temporalio.workflow


def _get_workflow_random() -> random.Random | None:
    if (
        temporalio.workflow.in_workflow()
        and not temporalio.workflow.unsafe.is_read_only()
    ):
        if (
            getattr(temporalio.workflow.instance(), "__temporal_otel_id_random", None)
            is None
        ):
            setattr(
                temporalio.workflow.instance(),
                "__temporal_otel_id_random",
                temporalio.workflow.new_random(),
            )
        return getattr(temporalio.workflow.instance(), "__temporal_otel_id_random")

    return None


class TemporalIdGenerator(IdGenerator):
    """OpenTelemetry ID generator that uses Temporal's deterministic random generator.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This generator uses Temporal's workflow-safe random number generator when
    inside a workflow execution, ensuring deterministic span and trace IDs
    across workflow replays. Falls back to standard random generation outside
    of workflows.
    """

    def __init__(self, id_generator: IdGenerator):
        """Initialize a TemporalIdGenerator."""
        self._id_generator = id_generator

    def generate_span_id(self) -> int:
        """Generate a span ID using Temporal's deterministic random when in workflow.

        Returns:
            A 64-bit span ID.
        """
        if workflow_random := _get_workflow_random():
            span_id = workflow_random.getrandbits(64)
            while span_id == INVALID_SPAN_ID:
                span_id = workflow_random.getrandbits(64)
            return span_id
        return self._id_generator.generate_span_id()

    def generate_trace_id(self) -> int:
        """Generate a trace ID using Temporal's deterministic random when in workflow.

        Returns:
            A 128-bit trace ID.
        """
        if workflow_random := _get_workflow_random():
            trace_id = workflow_random.getrandbits(128)
            while trace_id == INVALID_TRACE_ID:
                trace_id = workflow_random.getrandbits(128)
            return trace_id
        return self._id_generator.generate_trace_id()
