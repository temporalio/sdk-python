"""Tests for external storage functionality."""

import warnings
from collections.abc import Sequence

import pytest
from typing_extensions import Self

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    ActivitySerializationContext,
    DataConverter,
    JSONPlainPayloadConverter,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.exceptions import ApplicationError
from temporalio.extstore import (
    StorageConfig,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverContext,
    StorageWarning,
    _StorageReference,
)


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
        context: StorageDriverContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        self._store_calls += 1
        start_index = len(self._storage)

        entries = [
            (f"payload-{start_index + i}", payload.SerializeToString())
            for i, payload in enumerate(payloads)
        ]
        self._storage.update(entries)

        return [StorageDriverClaim(data={"key": key}) for key, _ in entries]

    async def retrieve(
        self,
        context: StorageDriverContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        self._retrieve_calls += 1

        def parse_claim(
            claim: StorageDriverClaim,
        ) -> Payload:
            key = claim.data["key"]
            if key not in self._storage:
                raise ApplicationError(
                    f"Payload not found for key '{key}'", non_retryable=True
                )
            payload = Payload()
            payload.ParseFromString(self._storage[key])
            return payload

        return [parse_claim(claim) for claim in claims]


class WorkflowIdFeatureFlagDriverSelector(WithSerializationContext):
    """Example selector that conditionally stores based on workflow ID feature flag.

    This example shows how a callable can implement WithSerializationContext if it
    needs to precompute data from the serialization context instead of doing it on
    every payload selection call.

    The feature flag in this example is a simple check on the workflow ID length, but in
    a real implementation this could be a call to a feature flag service or a lookup in a
    configuration store.
    """

    def __init__(self, driver: StorageDriver, enabled: bool = False):
        self._driver = driver
        self._enabled = enabled

    def __call__(
        self, _context: StorageDriverContext, _payload: Payload
    ) -> StorageDriver | None:
        return self._driver if self._enabled else None

    def with_context(self, context: SerializationContext) -> Self:
        workflow_id = None
        if isinstance(context, ActivitySerializationContext) and context.workflow_id:
            workflow_id = context.workflow_id
        if isinstance(context, WorkflowSerializationContext) and context.workflow_id:
            workflow_id = context.workflow_id

        # Create new instance with updated enabled flag and propagate context to inner driver
        driver = self._driver
        if isinstance(driver, WithSerializationContext):
            driver = driver.with_context(context)

        return type(self)(
            driver, WorkflowIdFeatureFlagDriverSelector.feature_flag_is_on(workflow_id)
        )

    @staticmethod
    def feature_flag_is_on(workflow_id: str | None) -> bool:
        """Mock implementation of a feature flag based on a workflow ID."""
        return workflow_id is not None and len(workflow_id) % 2 == 0


class TestDataConverterExternalStorage:
    """Tests for DataConverter with external storage."""

    async def test_extstore_encode_decode(self):
        """Test that large payloads are stored externally."""
        driver = InMemoryTestDriver()

        # Configure with 100-byte threshold
        converter = DataConverter(
            external_storage=StorageConfig(
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
            external_storage=StorageConfig(
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
        assert "key" in reference.driver_claim.data

    async def test_extstore_composite_conditional(self):
        """Test using multiple drivers based on size."""
        hot_driver = InMemoryTestDriver("hot-storage")
        cold_driver = InMemoryTestDriver("cold-storage")

        options = StorageConfig(
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

    async def test_extstore_serialization_context(self):
        driver = InMemoryTestDriver()

        # The payload should not be stored externally when it doesn't have a serialization context
        # or if the workflow ID doesn't end with "-extstore". This is an example of feature flagging
        # external storage using the workflow ID. This is an advanced secnario and requires the "can_store"
        # filter to be a WithSerializationContext.
        options = StorageConfig(
            drivers=[driver],
            driver_selector=WorkflowIdFeatureFlagDriverSelector(driver),
            payload_size_threshold=1024,
        )
        converter = DataConverter(external_storage=options)

        large_value = "c" * (1024 + 100)

        encoded_payloads = await converter.encode([large_value])
        assert len(encoded_payloads) == 1
        assert driver._store_calls == 0
        assert driver._retrieve_calls == 0

        decoded_values = await converter.decode(encoded_payloads, [str])
        assert len(decoded_values) == 1
        assert driver._store_calls == 0
        assert driver._store_calls == 0

        namespace = "my-ns"

        # Now has serialization context, but workflow ID does not end with "-extstore"
        converter = converter.with_context(
            WorkflowSerializationContext(
                namespace=namespace,
                workflow_id="odd-length-workflow-id1",
            )
        )

        encoded_payloads = await converter.encode([large_value])
        assert len(encoded_payloads) == 1
        assert driver._store_calls == 0
        assert driver._retrieve_calls == 0

        decoded_values = await converter.decode(encoded_payloads, [str])
        assert len(decoded_values) == 1
        assert driver._store_calls == 0
        assert driver._store_calls == 0

        # Now has serialization context with workflow ID ending with "-extstore"
        converter = converter.with_context(
            WorkflowSerializationContext(
                namespace=namespace,
                workflow_id="even-length-workflow-id1",
            )
        )

        encoded_payloads = await converter.encode([large_value])
        assert len(encoded_payloads) == 1
        assert driver._store_calls == 1
        assert driver._retrieve_calls == 0

        decoded_values = await converter.decode(encoded_payloads, [str])
        assert len(decoded_values) == 1
        assert driver._store_calls == 1
        assert driver._store_calls == 1


class NotFoundDriver(StorageDriver):
    """Driver that stores normally but raises non-retryable ApplicationError on retrieve."""

    def __init__(self, driver_name: str = "not-found-driver"):
        self._driver_name = driver_name
        self._storage: dict[str, bytes] = {}

    def name(self) -> str:
        return self._driver_name

    async def store(
        self,
        context: StorageDriverContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        entries = [
            (f"payload-{i}", payload.SerializeToString())
            for i, payload in enumerate(payloads)
        ]
        self._storage.update(entries)
        return [StorageDriverClaim(data={"key": key}) for key, _ in entries]

    async def retrieve(
        self,
        context: StorageDriverContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        assert len(claims) > 0, "NotFoundDriver expected claims to be provided"
        raise ApplicationError("Payload not found.", non_retryable=True)


class TestDriverError:
    """Tests for RuntimeError raised when a driver violates its contract."""

    async def test_encode_wrong_claim_count_raises_runtime_error(self):
        """store() returning fewer claims than payloads must raise RuntimeError."""

        class _NoClaimsDriver(InMemoryTestDriver):
            async def store(
                self, context: StorageDriverContext, payloads: Sequence[Payload]
            ) -> list[StorageDriverClaim]:
                return []

        driver = _NoClaimsDriver()
        converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[driver],
                payload_size_threshold=10,
            )
        )
        with pytest.raises(
            RuntimeError,
            match=f"Driver '{driver.name()}' returned 0 claims, expected 1",
        ):
            await converter.encode(["x" * 200])

    async def test_decode_wrong_payload_count_raises_runtime_error(self):
        """retrieve() returning fewer payloads than claims must raise RuntimeError."""
        good_converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[InMemoryTestDriver()],
                payload_size_threshold=10,
            )
        )
        encoded = await good_converter.encode(["x" * 200])

        class _NoPayloadsDriver(InMemoryTestDriver):
            async def retrieve(
                self,
                context: StorageDriverContext,
                claims: Sequence[StorageDriverClaim],
            ) -> list[Payload]:
                return []

        driver = _NoPayloadsDriver()
        bad_converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[driver],
                payload_size_threshold=10,
            )
        )
        with pytest.raises(
            RuntimeError,
            match=f"Driver '{driver.name()}' returned 0 payloads, expected 1",
        ):
            await bad_converter.decode(encoded, [str])


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

    async def test_dc_payload_codec_encodes_reference_payload(self):
        """DataConverter.payload_codec encodes the reference payload in workflow
        history but does NOT encode the bytes handed to the driver for storage."""
        driver = InMemoryTestDriver()
        dc_codec = RecordingPayloadCodec("binary/dc-encoded")

        converter = DataConverter(
            payload_codec=dc_codec,
            external_storage=StorageConfig(
                drivers=[driver],
                payload_size_threshold=50,
            ),
        )

        large_value = "x" * 200
        encoded = await converter.encode([large_value])
        assert len(encoded) == 1
        assert driver._store_calls == 1

        # The reference payload written to history must carry the dc_codec label.
        assert dc_codec.encoded_count == 1
        assert encoded[0].metadata.get("encoding") == b"binary/dc-encoded"

        # The bytes given to the driver must NOT carry the dc_codec label.
        stored_payload = Payload()
        stored_payload.ParseFromString(next(iter(driver._storage.values())))
        assert stored_payload.metadata.get("encoding") != b"binary/dc-encoded"
        assert stored_payload.metadata.get("encoding") == b"json/plain"

        # Round-trip must recover the original value.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large_value
        assert dc_codec.decoded_count == 1
        assert driver._retrieve_calls == 1

    async def test_external_converter_without_codec_does_not_encode_stored_bytes(self):
        """When DataConverter.payload_codec is set but StorageConfig.payload_codec
        is None, stored bytes are NOT encoded – even though
        DataConverter.payload_codec is active for the reference payload in history."""
        driver = InMemoryTestDriver()
        dc_codec = RecordingPayloadCodec("binary/dc-encoded")

        converter = DataConverter(
            payload_codec=dc_codec,
            external_storage=StorageConfig(
                drivers=[driver],
                payload_size_threshold=50,
            ),
        )

        large_value = "x" * 200
        encoded = await converter.encode([large_value])
        assert len(encoded) == 1
        assert driver._store_calls == 1

        # Reference payload in history is still encoded by DataConverter.payload_codec.
        assert dc_codec.encoded_count == 1
        assert encoded[0].metadata.get("encoding") == b"binary/dc-encoded"

        # Stored bytes are NOT encoded.
        stored_payload = Payload()
        stored_payload.ParseFromString(next(iter(driver._storage.values())))
        assert stored_payload.metadata.get("encoding") != b"binary/dc-encoded"
        assert stored_payload.metadata.get("encoding") == b"json/plain"

        # Round-trip.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large_value
        assert dc_codec.decoded_count == 1
        assert driver._retrieve_calls == 1

    async def test_external_converter_codec_independent_from_dc_codec(self):
        """When both DataConverter.payload_codec and StorageConfig.payload_codec
        are set, the reference payload in history uses DataConverter.payload_codec
        and the bytes stored by the driver use StorageConfig.payload_codec –
        independently."""
        driver = InMemoryTestDriver()
        dc_codec = RecordingPayloadCodec("binary/dc-encoded")
        ext_codec = RecordingPayloadCodec("binary/ext-encoded")

        converter = DataConverter(
            payload_codec=dc_codec,
            external_storage=StorageConfig(
                drivers=[driver],
                payload_size_threshold=50,
                payload_codec=ext_codec,
            ),
        )

        large_value = "x" * 200
        encoded = await converter.encode([large_value])
        assert len(encoded) == 1
        assert driver._store_calls == 1

        # Each codec was applied exactly once during encode.
        assert dc_codec.encoded_count == 1
        assert ext_codec.encoded_count == 1

        # Reference payload carries dc_codec's label.
        assert encoded[0].metadata.get("encoding") == b"binary/dc-encoded"

        # Stored bytes carry ext_codec's label – different from the reference.
        stored_payload = Payload()
        stored_payload.ParseFromString(next(iter(driver._storage.values())))
        assert stored_payload.metadata.get("encoding") == b"binary/ext-encoded"
        assert stored_payload.metadata.get("encoding") != encoded[0].metadata.get(
            "encoding"
        )

        # Round-trip must recover the original value using both codecs.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large_value
        assert dc_codec.decoded_count == 1
        assert ext_codec.decoded_count == 1
        assert driver._retrieve_calls == 1


class TestMultiDriver:
    """Tests for StorageConfig with multiple drivers."""

    async def test_no_selector_uses_first_driver_for_store(self):
        """Without a driver_selector the first driver in the list handles all
        store operations.  Additional drivers are never called for store."""
        first = InMemoryTestDriver("driver-first")
        second = InMemoryTestDriver("driver-second")

        converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[first, second],
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
            external_storage=StorageConfig(
                drivers=[driver_b],
                payload_size_threshold=50,
            )
        )
        large = "y" * 200
        encoded = await store_converter.encode([large])

        # Retrieve with driver-a listed first, driver-b second.
        # The "driver-b" name in the reference must route to driver-b.
        retrieve_converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[driver_a, driver_b],
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
        def selector(_ctx: object, payload: Payload) -> InMemoryTestDriver:
            return driver_a if payload.ByteSize() < 500 else driver_b

        converter = DataConverter(
            external_storage=StorageConfig(
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
            external_storage=StorageConfig(
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
        """A selector that returns a Driver whose name is not present in
        StorageConfig.drivers raises RuntimeError during encode."""
        registered = InMemoryTestDriver("registered")
        unregistered = InMemoryTestDriver("not-in-list")

        converter = DataConverter(
            external_storage=StorageConfig(
                drivers=[registered],
                driver_selector=lambda _ctx, _payload: unregistered,
                payload_size_threshold=50,
            )
        )

        with pytest.raises(RuntimeError):
            await converter.encode(["x" * 200])

    async def test_duplicate_driver_names_warns_and_first_wins_for_retrieval(self):
        """Registering two drivers with identical names emits StorageWarning.
        The first-registered driver with that name is kept in the name→driver
        map; subsequent duplicates are ignored.

        Both store (positional via drivers[0]) and retrieval (name-based map)
        therefore resolve to the same driver — no data is lost."""
        first = InMemoryTestDriver("dup-name")
        duplicate = InMemoryTestDriver("dup-name")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            converter = DataConverter(
                external_storage=StorageConfig(
                    drivers=[first, duplicate],
                    payload_size_threshold=50,
                )
            )

        storage_warnings = [w for w in caught if issubclass(w.category, StorageWarning)]
        assert len(storage_warnings) == 1
        assert "dup-name" in str(storage_warnings[0].message)

        # Store goes to first (drivers[0], positional).
        large = "x" * 200
        encoded = await converter.encode([large])
        assert first._store_calls == 1
        assert duplicate._store_calls == 0

        # Retrieval resolves "dup-name" to first (first-registered wins).
        # first has the data, so the round-trip succeeds.
        decoded = await converter.decode(encoded, [str])
        assert decoded[0] == large
        assert first._retrieve_calls == 1
        assert duplicate._retrieve_calls == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
