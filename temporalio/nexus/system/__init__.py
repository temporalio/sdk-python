"""Generated system Nexus service models.

This package contains code generated from Temporal's system Nexus schemas.
Higher-level ergonomic APIs may wrap these generated types.
"""

from collections.abc import Awaitable, Callable, Sequence

import temporalio.api.common.v1
import temporalio.converter

from . import _workflow_service_generated as generated
from ._workflow_service_generated import __temporal_nexus_payload_rewriters__

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

_SYSTEM_NEXUS_PAYLOAD_CONVERTER = temporalio.converter.JSONPlainPayloadConverter()


def get_payload_rewriter(
    service: str,
    operation: str,
) -> TemporalNexusPayloadRewriter | None:
    """Return the generated nested-payload rewriter for a system Nexus operation."""
    return __temporal_nexus_payload_rewriters__.get((service, operation))


def is_system_operation(service: str, operation: str) -> bool:
    """Return whether a Nexus operation uses the generated system envelope."""
    return get_payload_rewriter(service, operation) is not None


def get_payload_converter() -> temporalio.converter.EncodingPayloadConverter:
    """Return the fixed payload converter for system Nexus outer envelopes."""
    return _SYSTEM_NEXUS_PAYLOAD_CONVERTER


__all__ = (
    "generated",
    "get_payload_converter",
    "get_payload_rewriter",
    "is_system_operation",
)
