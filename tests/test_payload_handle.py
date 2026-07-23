"""Unit tests for PayloadHandle (Phase 1 prototype), server-free.

These exercise the converter-level behavior: a PayloadHandle[T] annotation
defers acquisition (external-storage retrieval, codec decode, deserialization)
until materialize() is awaited, and forwarding a handle re-emits its opaque
payload without downloading. The proof is the driver's retrieve-call count.
"""

from __future__ import annotations

import pickle
from collections.abc import Sequence

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    PayloadCodec,
    PayloadHandle,
)
from tests.test_extstore import InMemoryTestDriver

# A value large enough to be worth offloading; threshold=0 offloads everything.
_BIG = "x" * 1000


def _storage_converter(
    driver: InMemoryTestDriver, codec: PayloadCodec | None = None
) -> DataConverter:
    return DataConverter(
        payload_codec=codec,
        external_storage=ExternalStorage(drivers=[driver], payload_size_threshold=0),
    )


async def test_toplevel_reference_becomes_bound_handle() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)

    payloads = await dc.encode([_BIG])
    assert driver._store_calls == 1

    [handle] = await dc.decode(payloads, [PayloadHandle[str]])
    assert isinstance(handle, PayloadHandle)
    # No download happened just by receiving the handle.
    assert driver._retrieve_calls == 0

    assert await handle.materialize() == _BIG
    assert driver._retrieve_calls == 1


async def test_forward_only_handle_roundtrips_without_download() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    [reference] = await dc.encode([_BIG])

    # Decoding through the bare payload converter (no boundary binding, as in
    # the workflow sandbox) yields a forward-only handle.
    [handle] = dc.payload_converter.from_payloads([reference], [PayloadHandle[str]])
    assert isinstance(handle, PayloadHandle)

    with pytest.raises(RuntimeError, match="forward-only"):
        await handle.materialize()

    # Forwarding re-emits a byte-identical reference payload.
    [out] = dc.payload_converter.to_payloads([handle])
    assert out.SerializeToString() == reference.SerializeToString()
    assert driver._retrieve_calls == 0


async def test_non_handle_annotation_is_eager() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    payloads = await dc.encode([_BIG])

    # Default behavior is unchanged: a real-type hint materializes eagerly.
    [value] = await dc.decode(payloads, [str])
    assert value == _BIG
    assert not isinstance(value, PayloadHandle)
    assert driver._retrieve_calls == 1


async def test_tmprl1105_preserved_for_non_handle() -> None:
    driver = InMemoryTestDriver()
    [reference] = await _storage_converter(driver).encode([_BIG])

    # A reference decoded as a real type without storage still raises, exactly
    # as before this feature.
    with pytest.raises(RuntimeError, match="TMPRL1105"):
        await DataConverter().decode([reference], [str])


class _MarkerCodec(PayloadCodec):
    """Reversible codec that prefixes data and counts decode calls."""

    def __init__(self) -> None:
        self.decode_calls = 0

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata=dict(p.metadata), data=b"C" + p.data) for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.decode_calls += 1
        out = []
        for p in payloads:
            data = p.data[1:] if p.data.startswith(b"C") else p.data
            out.append(Payload(metadata=dict(p.metadata), data=data))
        return out


async def test_codec_deferred_until_materialize() -> None:
    driver = InMemoryTestDriver()
    codec = _MarkerCodec()
    dc = _storage_converter(driver, codec=codec)
    payloads = await dc.encode([_BIG])

    [handle] = await dc.decode(payloads, [PayloadHandle[str]])
    # The reference is not codec-decoded when the handle is produced.
    assert codec.decode_calls == 0

    assert await handle.materialize() == _BIG
    assert codec.decode_calls == 1


async def test_pickled_handle_is_forward_only() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    payloads = await dc.encode([_BIG])
    [handle] = await dc.decode(payloads, [PayloadHandle[str]])

    restored = pickle.loads(pickle.dumps(handle))
    assert isinstance(restored, PayloadHandle)
    with pytest.raises(RuntimeError, match="forward-only"):
        await restored.materialize()
    # The opaque payload survives, so a rehydrated handle can still be forwarded.
    # Compare with proto equality: re-parsing may reorder the metadata map, so
    # serialized bytes are not a reliable equality check here.
    [out] = dc.payload_converter.to_payloads([restored])
    assert out == payloads[0]
