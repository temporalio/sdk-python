"""Tests for external storage functionality."""

import asyncio
from collections.abc import Sequence

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    JSONPlainPayloadConverter,
    PayloadCodec,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
)
from temporalio.converter._extstore import _StorageReference
from temporalio.exceptions import ApplicationError


class InMemoryTestDriver(StorageDriver):
    """In-memory storage driver for testing."""

    def __init__(
        self,
        driver_name: str = "test-driver",
    ):
        self._driver_name = driver_name
        self._storage: dict[str, bytes] = {}
        self._store_calls = 0
        self._retrieve_calls = 0

    def name(self) -> str:
        return self._driver_name

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        self._store_calls += 1
        start_index = len(self._storage)

        entries = [
            (f"payload-{start_index + i}", payload.SerializeToString())
            for i, payload in enumerate(payloads)
        ]
        self._storage.update(entries)

        return [StorageDriverClaim(claim_data={"key": key}) for key, _ in entries]

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        self._retrieve_calls += 1

        def parse_claim(
            claim: StorageDriverClaim,
        ) -> Payload:
            key = claim.claim_data["key"]
            if key not in self._storage:
                raise ApplicationError(
                    f"Payload not found for key '{key}'", non_retryable=True
                )
            payload = Payload()
            payload.ParseFromString(self._storage[key])
            return payload

        return [parse_claim(claim) for claim in claims]


class TestDataConverterExternalStorage:
    """Tests for DataConverter with external storage."""

    async def test_extstore_encode_decode(self):
        """Test that large payloads are stored externally."""
        driver = InMemoryTestDriver()

        # Configure with 100-byte threshold
        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=100,
            )
        )

        # Small value should not be externalized
        small_value = "small"
        encoded_small = await converter.encode([small_value])
        assert len(encoded_small) == 1
        assert not encoded_small[0].external_payloads  # Not externalized
        assert driver._store_calls == 0

        # Large value should be externalized
        large_value = "x" * 200
        encoded_large = await converter.encode([large_value])
        assert len(encoded_large) == 1
        assert len(encoded_large[0].external_payloads) > 0  # Externalized
        assert driver._store_calls == 1

        # Decode large value
        decoded = await converter.decode(encoded_large, [str])
        assert len(decoded) == 1
        assert decoded[0] == large_value
        assert driver._retrieve_calls == 1

    async def test_extstore_reference_structure(self):
        """Test that external storage creates proper reference structure."""
        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[InMemoryTestDriver("test-driver")],
                payload_size_threshold=50,
            )
        )

        # Create large payload
        large_value = "x" * 100
        encoded = await converter.encode([large_value])

        # Verify reference structure
        reference_payload = encoded[0]
        assert len(reference_payload.external_payloads) > 0

        # The payload should contain a serialized _ExternalStorageReference
        # Deserialize it to verify structure using the same encoding
        claim_converter = JSONPlainPayloadConverter(
            encoding="json/external-storage-reference"
        )
        reference = claim_converter.from_payload(reference_payload, _StorageReference)

        assert isinstance(reference, _StorageReference)
        assert "test-driver" == reference.driver_name
        assert isinstance(reference.driver_claim, StorageDriverClaim)
        assert "key" in reference.driver_claim.claim_data

    async def test_extstore_composite_conditional(self):
        """Test using multiple drivers based on size."""
        hot_driver = InMemoryTestDriver("hot-storage")
        cold_driver = InMemoryTestDriver("cold-storage")

        options = ExternalStorage(
            drivers=[hot_driver, cold_driver],
            driver_selector=lambda context, payload: hot_driver
            if payload.ByteSize() < 500
            else cold_driver,
            payload_size_threshold=100,
        )
        converter = DataConverter(external_storage=options)

        # Small payload (not externalized)
        small = "x" * 50
        encoded_small = await converter.encode([small])
        assert not encoded_small[0].external_payloads
        assert hot_driver._store_calls == 0
        assert cold_driver._store_calls == 0

        # Medium payload (hot storage)
        medium = "x" * 200
        encoded_medium = await converter.encode([medium])
        assert len(encoded_medium[0].external_payloads) > 0
        assert hot_driver._store_calls == 1
        assert cold_driver._store_calls == 0

        # Large payload (cold storage)
        large = "x" * 2000
        encoded_large = await converter.encode([large])
        assert len(encoded_large[0].external_payloads) > 0
        assert hot_driver._store_calls == 1  # Unchanged
        assert cold_driver._store_calls == 1

        # Verify retrieval from correct drivers
        decoded_medium = await converter.decode(encoded_medium, [str])
        assert decoded_medium[0] == medium
        assert hot_driver._retrieve_calls == 1

        decoded_large = await converter.decode(encoded_large, [str])
        assert decoded_large[0] == large
        assert cold_driver._retrieve_calls == 1


class TestDriverError:
    """Tests for ValueError raised when a driver violates its contract."""

    async def test_encode_wrong_claim_count_raises_runtime_error(self):
        """store() returning fewer claims than payloads must raise ValueError."""

        class _NoClaimsDriver(InMemoryTestDriver):
            async def store(
                self, context: StorageDriverStoreContext, payloads: Sequence[Payload]
            ) -> list[StorageDriverClaim]:
                return []

        driver = _NoClaimsDriver()
        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=10,
            )
        )
        with pytest.raises(
            ValueError,
            match=f"Driver '{driver.name()}' returned 0 claims, expected 1",
        ):
            await converter.encode(["x" * 200])

    async def test_decode_wrong_payload_count_raises_runtime_error(self):
        """retrieve() returning fewer payloads than claims must raise ValueError."""
        good_converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[InMemoryTestDriver()],
                payload_size_threshold=10,
            )
        )
        encoded = await good_converter.encode(["x" * 200])

        class _NoPayloadsDriver(InMemoryTestDriver):
            async def retrieve(
                self,
                context: StorageDriverRetrieveContext,
                claims: Sequence[StorageDriverClaim],
            ) -> list[Payload]:
                return []

        driver = _NoPayloadsDriver()
        bad_converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=10,
            )
        )
        with pytest.raises(
            ValueError,
            match=f"Driver '{driver.name()}' returned 0 payloads, expected 1",
        ):
            await bad_converter.decode(encoded, [str])

    async def test_store_cancels_in_flight_driver_on_error(self):
        """When one driver raises during concurrent store, other in-flight drivers are cancelled."""
        store_cancelled = asyncio.Event()

        class _SleepingStoreDriver(InMemoryTestDriver):
            def __init__(self):
                super().__init__("sleeping")

            async def store(
                self,
                context: StorageDriverStoreContext,
                payloads: Sequence[Payload],
            ) -> list[StorageDriverClaim]:
                try:
                    await asyncio.sleep(float("inf"))
                except asyncio.CancelledError:
                    store_cancelled.set()
                    raise
                return []  # unreachable

        class _FailingStoreDriver(InMemoryTestDriver):
            def __init__(self):
                super().__init__("failing")

            async def store(
                self,
                context: StorageDriverStoreContext,
                payloads: Sequence[Payload],
            ) -> list[StorageDriverClaim]:
                raise ValueError(
                    "failed to store payloads because remote service is unavailable"
                )

        drivers = [_SleepingStoreDriver(), _FailingStoreDriver()]
        drivers_iter = iter(drivers)
        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=drivers,
                driver_selector=lambda ctx, p: next(drivers_iter),
                payload_size_threshold=None,
            )
        )

        with pytest.raises(
            ValueError,
            match="^failed to store payloads because remote service is unavailable$",
        ):
            await converter.encode(["payload_a", "payload_b"])

        assert store_cancelled.is_set()

    async def test_retrieve_cancels_in_flight_driver_on_error(self):
        """When one driver raises during concurrent retrieve, other in-flight drivers are cancelled."""
        retrieve_cancelled = asyncio.Event()

        class _SleepingRetrieveDriver(InMemoryTestDriver):
            def __init__(self):
                super().__init__("sleeping")

            async def retrieve(
                self,
                context: StorageDriverRetrieveContext,
                claims: Sequence[StorageDriverClaim],
            ) -> list[Payload]:
                try:
                    await asyncio.sleep(float("inf"))
                except asyncio.CancelledError:
                    retrieve_cancelled.set()
                    raise
                return []  # unreachable

        class _FailingRetrieveDriver(InMemoryTestDriver):
            def __init__(self):
                super().__init__("failing")

            async def retrieve(
                self,
                context: StorageDriverRetrieveContext,
                claims: Sequence[StorageDriverClaim],
            ) -> list[Payload]:
                raise ValueError(
                    "failed to retrieve a payload because the object key does not exist"
                )

        drivers: list[StorageDriver] = [
            _SleepingRetrieveDriver(),
            _FailingRetrieveDriver(),
        ]
        drivers_iter = iter(drivers)
        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=drivers,
                driver_selector=lambda ctx, p: next(drivers_iter),
                payload_size_threshold=None,
            )
        )
        encoded = await converter.encode(["payload_a", "payload_b"])

        with pytest.raises(
            ValueError,
            match="^failed to retrieve a payload because the object key does not exist$",
        ):
            await converter.decode(encoded, [str, str])

        assert retrieve_cancelled.is_set()


class RecordingPayloadCodec(PayloadCodec):
    """Codec that wraps each payload under a recognisable ``encoding`` label.

    Encode sets ``metadata["encoding"]`` to ``encoding_label`` and stores the
    serialised inner payload as ``data``.  Decode reverses that.  The call
    counters let tests assert exactly how many payloads each codec processed.
    """

    def __init__(self, encoding_label: str) -> None:
        self._encoding_label = encoding_label.encode()
        self.encoded_count = 0
        self.decoded_count = 0

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.encoded_count += len(payloads)
        results = []
        for p in payloads:
            wrapped = Payload()
            wrapped.metadata["encoding"] = self._encoding_label
            wrapped.data = p.SerializeToString()
            results.append(wrapped)
        return results

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.decoded_count += len(payloads)
        results = []
        for p in payloads:
            inner = Payload()
            inner.ParseFromString(p.data)
            results.append(inner)
        return results


class TestPayloadCodecWithExternalStorage:
    """Tests for interaction between DataConverter.payload_codec and external storage."""

    async def test_dc_payload_codec_encodes_stored_bytes(self):
        """DataConverter.payload_codec encodes the bytes handed to the driver
        for storage. The reference payload written to workflow history is NOT
        encoded by the DataConverter codec."""
        driver = InMemoryTestDriver()
        dc_codec = RecordingPayloadCodec("binary/dc-encoded")

        converter = DataConverter(
            payload_codec=dc_codec,
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=50,
            ),
        )

        large_value = "x" * 200
        encoded = await converter.encode([large_value])
        assert len(encoded) == 1
        assert driver._store_calls == 1

        # The reference payload written to history must NOT carry the dc_codec label.
        assert dc_codec.encoded_count == 1
        assert encoded[0].metadata.get("encoding") != b"binary/dc-encoded"

        # The bytes given to the driver must carry the dc_codec label.
        stored_payload = Payload()
        stored_payload.ParseFromString(next(iter(driver._storage.values())))
        assert stored_payload.metadata.get("encoding") == b"binary/dc-encoded"

        # Round-trip must recover the original value.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large_value
        assert dc_codec.decoded_count == 1
        assert driver._retrieve_calls == 1

    async def test_dc_payload_codec_does_not_encode_reference_payload(self):
        """The reference payload stored in workflow history is NOT encoded by
        DataConverter.payload_codec – encoding is applied to the stored bytes
        instead."""
        driver = InMemoryTestDriver()
        dc_codec = RecordingPayloadCodec("binary/dc-encoded")

        converter = DataConverter(
            payload_codec=dc_codec,
            external_storage=ExternalStorage(
                drivers=[driver],
                payload_size_threshold=50,
            ),
        )

        large_value = "x" * 200
        encoded = await converter.encode([large_value])
        assert len(encoded) == 1
        assert driver._store_calls == 1

        # Reference payload in history is NOT encoded by DataConverter.payload_codec.
        assert dc_codec.encoded_count == 1
        assert encoded[0].metadata.get("encoding") != b"binary/dc-encoded"

        # Stored bytes ARE encoded by DataConverter.payload_codec.
        stored_payload = Payload()
        stored_payload.ParseFromString(next(iter(driver._storage.values())))
        assert stored_payload.metadata.get("encoding") == b"binary/dc-encoded"

        # Round-trip.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large_value
        assert dc_codec.decoded_count == 1
        assert driver._retrieve_calls == 1


class TestMultiDriver:
    """Tests for ExternalStorage with multiple drivers."""

    async def test_selector_always_first_driver_handles_all_stores(self):
        """A selector that always picks the first driver routes all store
        operations there. The second driver is never called for store."""
        first = InMemoryTestDriver("driver-first")
        second = InMemoryTestDriver("driver-second")

        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[first, second],
                driver_selector=lambda _ctx, _p: first,
                payload_size_threshold=50,
            )
        )

        large = "x" * 200
        encoded = await converter.encode([large])

        assert first._store_calls == 1
        assert second._store_calls == 0

        # The reference in history names the first driver.
        ref = JSONPlainPayloadConverter(
            encoding="json/external-storage-reference"
        ).from_payload(encoded[0], _StorageReference)
        assert ref.driver_name == "driver-first"

        # Retrieval also goes to the first driver.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large
        assert first._retrieve_calls == 1
        assert second._retrieve_calls == 0

    async def test_no_selector_second_driver_is_retrieve_only(self):
        """A driver that is second in the list acts as a retrieve-only driver.
        References are resolved by name, not by position, so a payload stored
        by driver-b is retrieved correctly even when driver-a is listed first."""
        driver_a = InMemoryTestDriver("driver-a")
        driver_b = InMemoryTestDriver("driver-b")

        # Store with driver-b as the sole driver.
        store_converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver_b],
                payload_size_threshold=50,
            )
        )
        large = "y" * 200
        encoded = await store_converter.encode([large])

        # Retrieve with driver-a listed first, driver-b second.
        # The "driver-b" name in the reference must route to driver-b.
        retrieve_converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver_a, driver_b],
                driver_selector=lambda _ctx, _p: driver_a,
                payload_size_threshold=50,
            )
        )
        decoded = await retrieve_converter.decode(encoded, [str])
        assert decoded[0] == large
        assert driver_a._retrieve_calls == 0  # never consulted
        assert driver_b._retrieve_calls == 1

    async def test_selector_routes_payloads_to_different_drivers_in_single_batch(self):
        """When a selector routes different payloads to different drivers, a
        single encode([v1, v2, ...]) call batches payloads per driver so each
        driver receives exactly one store() call regardless of how many
        payloads are routed to it."""
        driver_a = InMemoryTestDriver("driver-a")
        driver_b = InMemoryTestDriver("driver-b")

        # Route payloads that serialise to < 500 bytes to driver_a, larger ones
        # to driver_b.
        def selector(_ctx: object, payload: Payload) -> StorageDriver:
            return driver_a if payload.ByteSize() < 500 else driver_b

        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver_a, driver_b],
                driver_selector=selector,
                payload_size_threshold=50,
            )
        )

        small_ext = "a" * 100  # above threshold, serialises well below 500 B
        large_ext = "b" * 1000  # serialises above 500 B

        # Encode both values in a single call — they should be batched per driver.
        encoded = await converter.encode([small_ext, large_ext])
        assert driver_a._store_calls == 1  # one batched call, not two individual ones
        assert driver_b._store_calls == 1

        # Full round-trip.
        decoded = await converter.decode(encoded, [str, str])
        assert decoded == [small_ext, large_ext]
        assert driver_a._retrieve_calls == 1
        assert driver_b._retrieve_calls == 1

    async def test_selector_returning_none_keeps_payload_inline(self):
        """A selector that returns None for a payload leaves it stored inline
        in workflow history rather than offloading it to any driver, even when
        the payload exceeds the size threshold."""
        driver = InMemoryTestDriver("driver-a")

        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver],
                driver_selector=lambda _ctx, _payload: None,
                payload_size_threshold=50,
            )
        )

        large = "x" * 200
        encoded = await converter.encode([large])

        assert driver._store_calls == 0
        assert len(encoded[0].external_payloads) == 0  # payload is inline

        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large
        assert driver._retrieve_calls == 0

    async def test_selector_returns_unregistered_driver_raises(self):
        """A selector that returns a driver instance not present in
        ExternalStorage.drivers raises ValueError during encode."""
        registered = InMemoryTestDriver("registered")
        unregistered = InMemoryTestDriver("unregistered")

        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[registered],
                driver_selector=lambda _ctx, _payload: unregistered,
                payload_size_threshold=50,
            )
        )

        with pytest.raises(ValueError):
            await converter.encode(["x" * 200])

    async def test_selector_dispatches_drivers_concurrently(self):
        started_a = asyncio.Event()
        started_b = asyncio.Event()

        class BarrierDriver(InMemoryTestDriver):
            def __init__(
                self, name: str, my_event: asyncio.Event, their_event: asyncio.Event
            ):
                super().__init__(name)
                self._my_event = my_event
                self._their_event = their_event

            async def store(
                self,
                context: StorageDriverStoreContext,
                payloads: Sequence[Payload],
            ) -> list[StorageDriverClaim]:
                self._my_event.set()
                await asyncio.wait_for(self._their_event.wait(), timeout=2.0)
                return await super().store(context, payloads)

        driver_a = BarrierDriver("driver-a", started_a, started_b)
        driver_b = BarrierDriver("driver-b", started_b, started_a)

        def selector(_ctx: object, payload: Payload) -> StorageDriver:
            return driver_a if payload.ByteSize() < 500 else driver_b

        converter = DataConverter(
            external_storage=ExternalStorage(
                drivers=[driver_a, driver_b],
                driver_selector=selector,
                payload_size_threshold=None,
            )
        )

        small_ext = "a" * 100  # routes to driver-a
        large_ext = "b" * 1000  # routes to driver-b

        # This will deadlock (and timeout) if the two store() calls are not
        # dispatched concurrently.
        encoded = await asyncio.wait_for(
            converter.encode([small_ext, large_ext]), timeout=5.0
        )

        decoded = await converter.decode(encoded, [str, str])
        assert decoded == [small_ext, large_ext]

    def test_multiple_drivers_without_selector_raises(self):
        """Registering more than one driver without a driver_selector raises
        ValueError immediately when constructing ExternalStorage."""
        first = InMemoryTestDriver("driver-a")
        second = InMemoryTestDriver("driver-b")

        with pytest.raises(
            ValueError,
            match=r"^ExternalStorage\.driver_selector must be specified if multiple drivers are registered\.$",
        ):
            ExternalStorage(
                drivers=[first, second],
                payload_size_threshold=50,
            )

    def test_duplicate_driver_names_raises(self):
        """Registering two drivers with identical names raises ValueError immediately
        when constructing ExternalStorage."""
        first = InMemoryTestDriver("dup-name")
        duplicate = InMemoryTestDriver("dup-name")

        with pytest.raises(
            ValueError,
            match=r"^ExternalStorage\.drivers contains multiple drivers with name 'dup-name'\. Each driver must have a unique name\.$",
        ):
            ExternalStorage(
                drivers=[first, duplicate],
                driver_selector=lambda _ctx, _p: first,
                payload_size_threshold=50,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
