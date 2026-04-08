"""Generated system Nexus service models.

This package contains code generated from Temporal's system Nexus schemas.
Higher-level ergonomic APIs may wrap these generated types.
"""

from collections.abc import Awaitable, Callable, Sequence

import temporalio.api.common.v1

from ._workflow_service_generated import (
    WorkflowService,
    WorkflowServiceSignalWithStartWorkflowExecutionInput,
    WorkflowServiceSignalWithStartWorkflowExecutionOutput,
    __temporal_nexus_payload_rewriters__,
)

TemporalNexusPayloadRewriter = Callable[
    [
        temporalio.api.common.v1.Payload,
        Callable[
            [Sequence[temporalio.api.common.v1.Payload]],
            Awaitable[list[temporalio.api.common.v1.Payload]],
        ],
        bool,
    ],
    Awaitable[temporalio.api.common.v1.Payload],
]


def get_payload_rewriter(
    service: str,
    operation: str,
) -> TemporalNexusPayloadRewriter | None:
    """Return the generated nested-payload rewriter for a system Nexus operation."""
    return __temporal_nexus_payload_rewriters__.get((service, operation))


__all__ = (
    "WorkflowService",
    "WorkflowServiceSignalWithStartWorkflowExecutionInput",
    "WorkflowServiceSignalWithStartWorkflowExecutionOutput",
    "get_payload_rewriter",
)
