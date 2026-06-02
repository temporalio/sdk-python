from __future__ import annotations

from typing import Protocol

from google.protobuf.internal.containers import RepeatedCompositeFieldContainer

from temporalio.api.common.v1.message_pb2 import Payload

PayloadSequence = list[Payload] | RepeatedCompositeFieldContainer[Payload]


class VisitorFunctions(Protocol):
    """Functions invoked by generated payload visitors."""

    async def visit_payload(self, payload: Payload) -> None:
        """Visit a single payload."""
        ...

    async def visit_payloads(self, payloads: PayloadSequence) -> None:
        """Visit a sequence of payloads together."""
        ...

    async def visit_system_nexus_envelope(self, payload: Payload) -> None:
        """Visit a recognized system Nexus envelope payload."""
        return None
