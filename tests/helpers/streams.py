"""Helpers for the tests of Streams."""

from collections.abc import Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
)

CodecCall = tuple[str, SerializationContext | None, int]


class ContextCodec(PayloadCodec, WithSerializationContext):
    """Wraps each payload with the kind of its serialization context as a key, and
    refuses to unwrap a payload with another key. ``calls`` logs the context and the
    payload count of each call.
    """

    def __init__(
        self,
        calls: list[CodecCall] | None = None,
        context: SerializationContext | None = None,
    ) -> None:
        self.calls: list[CodecCall] = [] if calls is None else calls
        self._context = context

    def with_context(self, context: SerializationContext) -> "ContextCodec":
        return ContextCodec(self.calls, context)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.calls.append(("encode", self._context, len(payloads)))
        return [wrapped(payload, self._context) for payload in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.calls.append(("decode", self._context, len(payloads)))
        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") == b"test/wrapped":
                key = wrapped(Payload(), self._context).metadata["context"]
                assert payload.metadata["context"] == key, (
                    f"{payload} isn't for {key!r}"
                )
                payload = Payload.FromString(payload.data)
            decoded.append(payload)
        return decoded


def wrapped(payload: Payload, context: SerializationContext | None = None) -> Payload:
    """Return ``payload`` as a :py:class:`ContextCodec` with ``context`` encodes it."""
    return Payload(
        metadata={
            "encoding": b"test/wrapped",
            "context": type(context).__name__.encode(),
        },
        data=payload.SerializeToString(),
    )
