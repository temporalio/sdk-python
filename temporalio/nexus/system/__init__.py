"""System Nexus operation helpers."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Sequence
from typing import Any

import temporalio.api.common.v1
import temporalio.converter
from temporalio.bridge._visitor_functions import VisitorFunctions
from temporalio.converter import BinaryProtoPayloadConverter, CompositePayloadConverter

TEMPORAL_SYSTEM_ENDPOINT = "__temporal_system"
_user_payload_converter: contextvars.ContextVar[
    temporalio.converter.PayloadConverter | None
] = contextvars.ContextVar("temporal-system-nexus-user-payload-converter", default=None)


@contextlib.contextmanager
def user_payload_converter_context(
    payload_converter: temporalio.converter.PayloadConverter,
) -> Iterator[None]:
    """Set the user payload converter for system Nexus model conversion."""
    token = _user_payload_converter.set(payload_converter)
    try:
        yield
    finally:
        _user_payload_converter.reset(token)


def current_user_payload_converter() -> temporalio.converter.PayloadConverter:
    """Return the active user payload converter for system Nexus model conversion."""
    payload_converter = _user_payload_converter.get()
    if payload_converter is None:
        raise RuntimeError("System Nexus user payload converter context is not active")
    return payload_converter


class _SystemNexusOuterPayloadConverter(CompositePayloadConverter):
    """Payload converter for system Nexus outer proto envelopes."""

    def __init__(self) -> None:
        """Create a payload converter for system Nexus outer envelopes."""
        super().__init__(BinaryProtoPayloadConverter())


class SystemNexusPayloadConverter(temporalio.converter.PayloadConverter):
    """Payload converter for system Nexus outer envelopes."""

    _user_payload_converter: temporalio.converter.PayloadConverter
    _outer_payload_converter: temporalio.converter.PayloadConverter

    def __init__(self, user_payload_converter: temporalio.converter.PayloadConverter) -> None:
        """Create a payload converter for system Nexus outer envelopes."""
        self._user_payload_converter = user_payload_converter
        self._outer_payload_converter = (
            temporalio.converter.TemporalIntermediatePayloadConverter.wrap(
                _SystemNexusOuterPayloadConverter()
            )
        )

    def to_payloads(
        self, values: Sequence[Any]
    ) -> list[temporalio.api.common.v1.Payload]:
        """See base class."""
        with user_payload_converter_context(self._user_payload_converter):
            return self._outer_payload_converter.to_payloads(values)

    def from_payloads(
        self,
        payloads: Sequence[temporalio.api.common.v1.Payload],
        type_hints: list[type] | None = None,
    ) -> list[Any]:
        """See base class."""
        with user_payload_converter_context(self._user_payload_converter):
            return self._outer_payload_converter.from_payloads(payloads, type_hints)


def is_system_endpoint(endpoint: str) -> bool:
    """Return whether a Nexus endpoint is the Temporal system endpoint."""
    return endpoint == TEMPORAL_SYSTEM_ENDPOINT


async def maybe_visit_payload(
    endpoint: str,
    payload: temporalio.api.common.v1.Payload,
    visitor_functions: VisitorFunctions,
    skip_search_attributes: bool,
) -> temporalio.api.common.v1.Payload | None:
    """Visit nested payloads if the payload is for the Temporal system endpoint."""
    if not is_system_endpoint(endpoint):
        return None

    payload_converter = _SystemNexusOuterPayloadConverter()
    value = payload_converter.from_payload(payload)
    from ._payload_visitor import PayloadVisitor

    await PayloadVisitor(skip_search_attributes=skip_search_attributes).visit(
        visitor_functions, value
    )
    return payload_converter.to_payload(value)


def get_payload_converter(
    user_payload_converter: temporalio.converter.PayloadConverter,
) -> temporalio.converter.PayloadConverter:
    """Return the fixed payload converter for system Nexus outer envelopes."""
    return SystemNexusPayloadConverter(user_payload_converter)


__all__ = [
    "TEMPORAL_SYSTEM_ENDPOINT",
    "current_user_payload_converter",
    "get_payload_converter",
    "is_system_endpoint",
    "maybe_visit_payload",
    "SystemNexusPayloadConverter",
    "user_payload_converter_context",
]
