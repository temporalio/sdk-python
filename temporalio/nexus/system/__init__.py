"""System Nexus operation helpers."""

from __future__ import annotations

import typing

import google.protobuf.message
import nexusrpc

import temporalio.api.common.v1
import temporalio.converter
from temporalio.bridge._visitor_functions import VisitorFunctions
from temporalio.converter import BinaryProtoPayloadConverter, CompositePayloadConverter
from temporalio.nexus.system import workflow_service


class SystemNexusPayloadConverter(CompositePayloadConverter):
    """Payload converter for system Nexus outer envelopes."""

    def __init__(self) -> None:
        """Create a payload converter for system Nexus outer envelopes."""
        super().__init__(BinaryProtoPayloadConverter())


def _operation(
    service: str, operation: str
) -> nexusrpc.Operation[typing.Any, typing.Any] | None:
    return workflow_service.__nexus_operation_registry__.get((service, operation))


async def visit_payload(
    service: str,
    operation: str,
    payload: temporalio.api.common.v1.Payload,
    visitor_functions: VisitorFunctions,
    skip_search_attributes: bool,
) -> temporalio.api.common.v1.Payload | None:
    """Visit nested payloads inside a recognized system Nexus envelope."""
    operation_def = _operation(service, operation)
    if operation_def is None:
        return None
    input_type = operation_def.input_type
    if not (
        isinstance(input_type, type)
        and issubclass(input_type, google.protobuf.message.Message)
    ):
        return None

    payload_converter = get_payload_converter()
    value = payload_converter.from_payload(payload, input_type)
    from ._payload_visitor import PayloadVisitor

    await PayloadVisitor(skip_search_attributes=skip_search_attributes).visit(
        visitor_functions, value
    )
    return payload_converter.to_payload(value)


def is_system_operation(service: str, operation: str) -> bool:
    """Return whether a Nexus operation uses a generated system envelope."""
    return _operation(service, operation) is not None


def get_payload_converter() -> temporalio.converter.PayloadConverter:
    """Return the fixed payload converter for system Nexus outer envelopes."""
    return SystemNexusPayloadConverter()


__all__ = [
    "get_payload_converter",
    "is_system_operation",
    "SystemNexusPayloadConverter",
    "visit_payload",
]
