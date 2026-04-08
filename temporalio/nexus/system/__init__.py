"""Generated system Nexus service models.

This package contains code generated from Temporal's system Nexus schemas.
Higher-level ergonomic APIs may wrap these generated types.
"""

from collections.abc import Awaitable, Callable

import temporalio.api.common.v1
import temporalio.converter

from ._workflow_service_generated import (
    WorkflowService,
    WorkflowServiceSignalWithStartWorkflowExecutionInput,
    WorkflowServiceSignalWithStartWorkflowExecutionOutput,
    __temporal_nexus_payload_codec_rewriters__,
)

TemporalNexusPayloadCodecRewriter = Callable[
    [
        temporalio.api.common.v1.Payload,
        temporalio.converter.PayloadCodec | None,
    ],
    Awaitable[temporalio.api.common.v1.Payload],
]


def get_payload_codec_rewriter(
    service: str,
    operation: str,
) -> TemporalNexusPayloadCodecRewriter | None:
    """Return the generated payload codec rewriter for a system Nexus operation."""
    return __temporal_nexus_payload_codec_rewriters__.get((service, operation))


__all__ = (
    "WorkflowService",
    "WorkflowServiceSignalWithStartWorkflowExecutionInput",
    "WorkflowServiceSignalWithStartWorkflowExecutionOutput",
    "get_payload_codec_rewriter",
)
