import random
from typing import Callable

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

    def _get_rand_bits(self) -> Callable[[int], int]:
        if (
            temporalio.workflow.in_workflow()
            and not temporalio.workflow.unsafe.is_read_only()
        ):
            # Cache the random on the workflow instance because this IdGenerator is created outside of the workflow
            # but the random should be reseeded for each workflow task
            if (
                getattr(
                    temporalio.workflow.instance(), "__temporal_otel_id_random", None
                )
                is None
            ):
                setattr(
                    temporalio.workflow.instance(),
                    "__temporal_otel_id_random",
                    temporalio.workflow.new_random(),
                )
            return getattr(
                temporalio.workflow.instance(), "__temporal_otel_id_random"
            ).getrandbits

        return random.getrandbits

    def generate_span_id(self) -> int:
        """Generate a span ID using Temporal's deterministic random when in workflow.

        Returns:
            A 64-bit span ID.
        """
        get_rand_bits = self._get_rand_bits()
        span_id = get_rand_bits(64)
        while span_id == INVALID_SPAN_ID:
            span_id = get_rand_bits(64)
        return span_id

    def generate_trace_id(self) -> int:
        """Generate a trace ID using Temporal's deterministic random when in workflow.

        Returns:
            A 128-bit trace ID.
        """
        get_rand_bits = self._get_rand_bits()
        trace_id = get_rand_bits(128)
        while trace_id == INVALID_TRACE_ID:
            trace_id = get_rand_bits(128)
        return trace_id
