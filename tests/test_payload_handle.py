"""Unit tests for ValueHandle (Phase 1 prototype), server-free.

These exercise the converter-level behavior: a ValueHandle[T] annotation
defers acquisition (external-storage retrieval, codec decode, deserialization)
until the value is acquired at a boundary via DataConverter.get_handle_value,
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
    assert await dc.get_handle_value(handle) == _BIG
    assert driver._retrieve_calls == 1


async def test_handle_is_data_only_on_the_wire_and_forwards_without_download() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    [reference] = await dc.encode([_BIG])

    # A handle is data-only *on the wire*: it captures no converter, so holding it
    # downloads nothing and forwarding re-emits its opaque reference unchanged. The
    # get_value method lives on the handle (like other SDK handles' value getters),
    # but it reaches a converter at call time rather than capturing one.
    [handle] = dc.payload_converter.from_payloads([reference], [ValueHandle[str]])
    assert isinstance(handle, ValueHandle)
    assert driver._retrieve_calls == 0

    # Forwarding re-emits a byte-identical reference payload, still no download.
    [out] = dc.payload_converter.to_payloads([handle])
    assert out.SerializeToString() == reference.SerializeToString()
    assert driver._retrieve_calls == 0

    # Because the handle captured no converter, get_value() cannot acquire on its own
    # outside any execution context: it raises rather than silently downloading.
    with pytest.raises(RuntimeError):
        await handle.get_value()
    assert driver._retrieve_calls == 0

    # The value is still acquirable through an explicit converter, the machinery
    # get_value() delegates to inside an activity or client context.
    assert await dc.get_handle_value(handle) == _BIG
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

    assert await dc.get_handle_value(handle) == _BIG
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
    assert await dc.get_handle_value(restored) == _BIG


async def test_create_value_handle_defers_store_until_commit() -> None:
    driver = InMemoryTestDriver()
    # A realistic threshold: the value offloads, its small reference does not.
    dc = DataConverter(
        external_storage=ExternalStorage(drivers=[driver], payload_size_threshold=1024)
    )
    value = "x" * 4096

    # Creating a handle stores nothing: the store is deferred to commit (encode).
    # create_value_handle does no I/O at call time (the convert is synchronous).
    handle = await dc.create_value_handle(value, metadata={"pages": "42"})
    assert isinstance(handle, ValueHandle)
    assert driver._store_calls == 0
    # Metadata is known without any store.
    assert handle.metadata == {"pages": "42"}

    # Encoding the handle, as at result/input commit, is where the store happens.
    [payload] = await dc.encode([handle])
    assert driver._store_calls == 1

    # The committed payload is a realized reference carrying the metadata: a
    # consumer probes the metadata without downloading, then resolves to the value.
    [realized] = await dc.decode([payload], [ValueHandle[str]])
    assert realized.metadata == {"pages": "42"}
    assert driver._retrieve_calls == 0
    assert await dc.get_handle_value(realized) == value
    assert driver._retrieve_calls == 1


async def test_pending_handle_dropped_without_commit_is_never_stored() -> None:
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)

    # A producer that creates a handle but never commits it (e.g. an activity that
    # faults before returning) uploads nothing, so no external-storage blob is
    # orphaned.
    _ = await dc.create_value_handle(_BIG, metadata={"pages": "42"})
    assert driver._store_calls == 0


class _PassthroughCodec(PayloadCodec):
    """No-op codec used to exercise the context-requirement declaration."""

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return list(payloads)


async def test_decode_requires_context_declaration() -> None:
    # The declaration certifies whether correct decode/deserialize is *invariant*
    # to the serialization context. It is the single boolean the forward-time
    # wrap-or-not decision reads: False means a handle can forward by reference
    # across contexts with no envelope; True means the origin must be carried.

    # No codec + default converter: nothing requires the context, path is clear.
    dc = DataConverter()
    assert dc.payload_converter.deserialize_requires_context is False
    assert dc.decode_requires_context is False

    class _ContextCodec(_PassthroughCodec):
        decode_requires_context = True

    # An undeclared codec is optimistically assumed context-invariant (the default):
    # most codecs are, and a context-dependent one must opt in rather than the many
    # context-invariant ones having to opt out.
    assert _PassthroughCodec().decode_requires_context is False
    assert DataConverter(payload_codec=_PassthroughCodec()).decode_requires_context is False

    # A context-requiring codec (e.g. context-keyed encryption) opts in and flips
    # the aggregate, so forwarding across contexts would need the origin carried.
    assert DataConverter(payload_codec=_ContextCodec()).decode_requires_context is True


async def test_forwarding_handle_gated_by_context_requirement() -> None:
    # Context-independent (the default): forwarding a handle by reference is safe,
    # so encode does not fire the guard and the forward succeeds.
    driver = InMemoryTestDriver()
    dc = _storage_converter(driver)
    [reference] = await dc.encode([_BIG])
    [handle] = await dc.decode([reference], [ValueHandle[str]])
    forwarded = await dc.encode([handle])
    assert len(forwarded) == 1

    # Context-dependent: forwarding the same handle by reference would decode under
    # the wrong context at its destination, and the ContextBoundPayload envelope
    # that would carry the origin is not implemented, so encode fails fast.
    class _ContextCodec(_PassthroughCodec):
        decode_requires_context = True

    dc2 = DataConverter(
        payload_codec=_ContextCodec(),
        external_storage=ExternalStorage(
            drivers=[InMemoryTestDriver()], payload_size_threshold=0
        ),
    )
    [ref2] = await dc2.encode([_BIG])
    [handle2] = await dc2.decode([ref2], [ValueHandle[str]])
    with pytest.raises(RuntimeError, match="TMPRL1108"):
        await dc2.encode([handle2])

    # A *pending* handle (a fresh produce, not a forward) is unaffected: it is
    # realized and stored under this context regardless of the declaration.
    pending = await dc2.create_value_handle(_BIG)
    [_produced] = await dc2.encode([pending])
