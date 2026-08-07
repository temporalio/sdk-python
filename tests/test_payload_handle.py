"""Unit tests for ValueHandle (Phase 1 prototype), server-free.

These exercise the converter-level behavior: a ValueHandle[T] annotation
defers acquisition (external-storage retrieval, codec decode, deserialization)
until the value is acquired at a boundary via DataConverter.resolve_value_handle,
and forwarding a handle re-emits its opaque payload without downloading. The
proof is the driver's retrieve-call count.
"""

from __future__ import annotations

import pickle
from collections.abc import Sequence

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.common import ValueHandle
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    PayloadCodec,
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


async def test_toplevel_reference_becomes_handle() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)

    payloads = await dc.encode([_BIG])
    assert driver._store_calls == 1

    [handle] = await dc.decode(payloads, [ValueHandle[str]])
    assert isinstance(handle, ValueHandle)
    # No download happened just by receiving the handle.
    assert driver._retrieve_calls == 0

    # The value is acquired at the boundary, through the converter.
    assert await dc.resolve_value_handle(handle) == _BIG
    assert driver._retrieve_calls == 1


async def test_handle_is_data_only_and_forwards_without_download() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    [reference] = await dc.encode([_BIG])

    # A handle is a data-only value: holding it downloads nothing, and it owns
    # no acquire behavior (acquisition is a boundary operation, not a method on
    # the handle). Forward-only-ness lives on the workflow surface, which simply
    # does not expose resolve_value_handle, not in the handle's state.
    [handle] = dc.payload_converter.from_payloads([reference], [ValueHandle[str]])
    assert isinstance(handle, ValueHandle)
    assert not hasattr(handle, "materialize")
    assert not hasattr(handle, "resolve_value_handle")
    assert driver._retrieve_calls == 0

    # Forwarding re-emits a byte-identical reference payload, still no download.
    [out] = dc.payload_converter.to_payloads([handle])
    assert out.SerializeToString() == reference.SerializeToString()
    assert driver._retrieve_calls == 0

    # The value is acquired only through a boundary converter.
    assert await dc.resolve_value_handle(handle) == _BIG
    assert driver._retrieve_calls == 1


async def test_non_handle_annotation_is_eager() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    payloads = await dc.encode([_BIG])

    # Default behavior is unchanged: a real-type hint materializes eagerly.
    [value] = await dc.decode(payloads, [str])
    assert value == _BIG
    assert not isinstance(value, ValueHandle)
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


async def test_codec_deferred_until_acquired() -> None:
    driver = InMemoryTestDriver()
    codec = _MarkerCodec()
    dc = _storage_converter(driver, codec=codec)
    payloads = await dc.encode([_BIG])

    [handle] = await dc.decode(payloads, [ValueHandle[str]])
    # The reference is not codec-decoded when the handle is produced.
    assert codec.decode_calls == 0

    assert await dc.resolve_value_handle(handle) == _BIG
    assert codec.decode_calls == 1


async def test_pickled_handle_survives_and_forwards() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    payloads = await dc.encode([_BIG])
    [handle] = await dc.decode(payloads, [ValueHandle[str]])

    restored = pickle.loads(pickle.dumps(handle))
    assert isinstance(restored, ValueHandle)
    # The opaque payload survives, so a rehydrated handle can still be forwarded
    # and acquired at a boundary. Compare with proto equality: re-parsing may
    # reorder the metadata map, so serialized bytes are not a reliable check.
    [out] = dc.payload_converter.to_payloads([restored])
    assert out == payloads[0]
    assert await dc.resolve_value_handle(restored) == _BIG


async def test_create_value_handle_stores_once_with_probeable_metadata() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)

    # Producing a handle from a value stores it once and wraps the reference.
    handle = await dc.create_value_handle(_BIG, metadata={"pages": "42"})
    assert isinstance(handle, ValueHandle)
    assert driver._store_calls == 1
    assert driver._retrieve_calls == 0

    # Metadata is readable without acquiring (downloading) the value.
    assert handle.metadata == {"pages": "42"}
    assert driver._retrieve_calls == 0

    # The value round-trips through a boundary acquire.
    assert await dc.resolve_value_handle(handle) == _BIG
    assert driver._retrieve_calls == 1
