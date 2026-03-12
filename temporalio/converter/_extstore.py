"""External payload storage support for offloading payloads to external storage systems."""

from __future__ import annotations

import asyncio
import dataclasses
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import Self

from temporalio.api.common.v1 import Payload, Payloads
from temporalio.converter import (
    JSONPlainPayloadConverter,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
)


@dataclass(frozen=True)
class StorageDriverClaim:
    """Claim for an externally stored payload.

    .. warning::
           This API is experimental.
    """

    data: Mapping[str, str]
    """Driver-defined data for identifying and retrieving an externally stored payload."""


@dataclass(frozen=True)
class StorageDriverContext:
    """Context passed to :class:`StorageDriver` and :class:`DriverSelector` calls.

    .. warning::
           This API is experimental.
    """

    serialization_context: SerializationContext | None = None
    """The serialization context active when this driver operation was initiated,
    or ``None`` if no context has been set.
    """


class StorageDriver(ABC):
    """Base driver for storing and retrieve payloads from external storage systems.

    .. warning::
           This API is experimental.
    """

    @abstractmethod
    def name(self) -> str:
        """Returns the name of this driver instance. A driver may allow its name
        to be parameterized at construction time so that multiple instances of
        the same driver class can coexist in :attr:`ExternalStorage.drivers` with
        distinct names.
        """
        raise NotImplementedError

    def type(self) -> str:
        """Returns the type of the storage driver. This string should be the same
        across all instantiations of the same driver class. This allows the equivalent
        driver implementation in different languages to be named the same.

        Defaults to the class name. Subclasses may override this to return a
        stable, language-agnostic identifier.
        """
        return type(self).__name__

    @abstractmethod
    async def store(
        self,
        context: StorageDriverContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        """Stores payloads in external storage and returns a :class:`StorageDriverClaim`
        for each one. The returned list must be the same length as ``payloads``.
        """
        raise NotImplementedError

    @abstractmethod
    async def retrieve(
        self,
        context: StorageDriverContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        """Retrieves payloads from external storage for the given :class:`StorageDriverClaim`
        list. The returned list must be the same length as ``claims``.

        Raise :class:`PayloadNotFoundError` when a retrieval attempt confirms
        that a payload is absent from storage. This signals an unrecoverable
        condition that will fail the workflow rather than retrying the workflow
        task.
        """
        raise NotImplementedError


class StorageWarning(RuntimeWarning):
    """Warning for external storage issues.

    .. warning::
           This API is experimental.
    """


@dataclass(frozen=True)
class _StorageReference:
    driver_name: str
    driver_claim: StorageDriverClaim


@dataclass(frozen=True)
class ExternalStorage(WithSerializationContext):
    """Configuration for external storage behavior.

    .. warning::
           This API is experimental.
    """

    drivers: Sequence[StorageDriver]
    """Drivers available for storing and retrieving payloads. At least one
    driver must be provided.

    When no :attr:`driver_selector` is set, the first driver in this list is
    used for all store operations. Additional drivers may be included solely to
    support retrieval — for example, to download payloads that remote callers
    uploaded to an external storage system that is not your primary store
    driver. Drivers in this list are looked up by :meth:`Driver.name` during
    retrieval, so each driver must have a unique name.
    """

    driver_selector: Callable[[StorageDriverContext, Payload], str | None] | None = None
    """Controls which driver stores a given payload. A callable of the form
    ``(StorageDriverContext, Payload) -> str | None`` that returns the name of
    the driver to use, or ``None`` to leave the payload stored inline.

    When ``None``, the first driver in :attr:`drivers` is used for all store
    operations.
    """

    payload_size_threshold: int | None = 256 * 1024
    """Minimum payload size in bytes before external storage is considered.
    Defaults to 256 KiB. Set to ``None`` to consider every payload for
    external storage regardless of size.
    """

    payload_codec: PayloadCodec | None = None
    """Optional codec applied to payloads before they are handed to a
    :class:`StorageDriver` for storage, and after they are retrieved. When ``None``,
    payloads are stored as-is by the driver.
    """

    _driver_map: dict[str, StorageDriver] = dataclasses.field(
        init=False, repr=False, compare=False
    )
    """Name-keyed index of :attr:`drivers`, built at construction time."""

    _context: SerializationContext | None = dataclasses.field(
        init=False, default=None, repr=False, compare=False
    )

    _claim_converter: ClassVar[JSONPlainPayloadConverter] = JSONPlainPayloadConverter(
        encoding="json/external-storage-reference"
    )

    def __post_init__(self) -> None:
        """Validate drivers and build the internal name-keyed driver map.

        Raises :exc:`ValueError` if any two drivers share the same name.
        """
        driver_map: dict[str, StorageDriver] = {}
        for driver in self.drivers:
            name = driver.name()
            if name in driver_map:
                raise ValueError(
                    f"ExternalStorage.drivers contains multiple drivers with name '{name}'. "
                    "Each driver must have a unique name."
                )
            driver_map[name] = driver
        object.__setattr__(self, "_driver_map", driver_map)

    def with_context(self, context: SerializationContext) -> Self:
        """Return a copy of these options with the serialization context applied."""
        payload_codec = self.payload_codec
        if isinstance(payload_codec, WithSerializationContext):
            payload_codec = payload_codec.with_context(context)
        result = dataclasses.replace(self, payload_codec=payload_codec)
        object.__setattr__(result, "_context", context)
        return result

    def _select_driver(
        self, context: StorageDriverContext, payload: Payload
    ) -> StorageDriver | None:
        """Returns the driver to use for this payload, or None to pass through."""
        selector = self.driver_selector
        if selector is None:
            return self.drivers[0] if self.drivers else None
        driver_name = selector(context, payload)
        if driver_name is None:
            return None
        driver = self._driver_map.get(driver_name)
        if driver is None:
            raise ValueError(f"No driver found with name '{driver_name}'")
        return driver

    def _get_driver_by_name(self, name: str) -> StorageDriver:
        """Looks up a driver by name, raising :class:`ValueError` if not found."""
        driver = self._driver_map.get(name)
        if driver is None:
            raise ValueError(f"No driver found with name '{name}'")
        return driver

    async def store_payload(self, payload: Payload) -> Payload:
        size_bytes = payload.ByteSize()
        if (
            self.payload_size_threshold is not None
            and size_bytes < self.payload_size_threshold
        ):
            return payload

        context = StorageDriverContext(serialization_context=self._context)

        driver = self._select_driver(context, payload)
        if driver is None:
            return payload

        encoded_payload = payload
        if self.payload_codec:
            encoded_payload = (await self.payload_codec.encode([payload]))[0]

        claims = await driver.store(context, [encoded_payload])

        self._validate_claim_length(claims, expected=1, driver=driver)

        reference = _StorageReference(
            driver_name=driver.name(),
            driver_claim=claims[0],
        )
        reference_payload = self._claim_converter.to_payload(reference)
        if reference_payload is None:
            raise ValueError(
                f"Failed to serialize storage reference for driver '{driver.name()}'"
            )
        reference_payload.external_payloads.add().size_bytes = (
            encoded_payload.ByteSize()
        )
        return reference_payload

    async def store_payloads(self, payloads: Payloads):
        stored_payloads = await self.store_payload_sequence(payloads.payloads)
        for i, payload in enumerate(stored_payloads):
            payloads.payloads[i].CopyFrom(payload)

    async def store_payload_sequence(
        self,
        payloads: Sequence[Payload],
    ) -> list[Payload]:
        if len(payloads) == 1:
            return [await self.store_payload(payloads[0])]

        results = list(payloads)
        context = StorageDriverContext(serialization_context=self._context)

        to_store: list[tuple[int, Payload, StorageDriver]] = []
        for index, payload in enumerate(payloads):
            size_bytes = payload.ByteSize()
            if (
                self.payload_size_threshold is not None
                and size_bytes < self.payload_size_threshold
            ):
                continue
            driver = self._select_driver(context, payload)
            if driver is None:
                continue
            to_store.append((index, payload, driver))

        if not to_store:
            return results

        payloads_to_encode = [payload for _, payload, _ in to_store]
        encoded_payloads = payloads_to_encode
        if self.payload_codec:
            encoded_payloads = await self.payload_codec.encode(payloads_to_encode)

        driver_groups: dict[StorageDriver, list[tuple[int, Payload]]] = {}
        for i, (orig_index, _, driver) in enumerate(to_store):
            driver_groups.setdefault(driver, []).append(
                (orig_index, encoded_payloads[i])
            )

        driver_group_list = list(driver_groups.items())

        all_claims = await asyncio.gather(
            *(
                driver.store(context, [p for _, p in indexed_payloads])
                for driver, indexed_payloads in driver_group_list
            )
        )

        for (driver, indexed_payloads), claims in zip(driver_group_list, all_claims):
            indices = [idx for idx, _ in indexed_payloads]
            sizes = [p.ByteSize() for _, p in indexed_payloads]

            self._validate_claim_length(claims, expected=len(indices), driver=driver)

            for i, claim in enumerate(claims):
                reference = _StorageReference(
                    driver_name=driver.name(),
                    driver_claim=claim,
                )
                reference_payload = self._claim_converter.to_payload(reference)
                if reference_payload is None:
                    raise ValueError(
                        f"Failed to serialize storage reference for driver '{driver.name()}'"
                    )
                reference_payload.external_payloads.add().size_bytes = sizes[i]
                results[indices[i]] = reference_payload

        return results

    async def retrieve_payload(self, payload: Payload) -> Payload:
        if len(self.drivers) == 0:
            if len(payload.external_payloads) > 0:
                warnings.warn(
                    "ExternalStorage.drivers is empty, but detected external storage references.",
                    category=StorageWarning,
                )
            return payload

        if len(payload.external_payloads) == 0:
            return payload

        reference = self._claim_converter.from_payload(payload, _StorageReference)
        if not isinstance(reference, _StorageReference):
            return payload

        driver = self._get_driver_by_name(reference.driver_name)
        context = StorageDriverContext(serialization_context=self._context)

        stored_payloads = await driver.retrieve(context, [reference.driver_claim])

        self._validate_payload_length(stored_payloads, expected=1, driver=driver)

        if self.payload_codec:
            stored_payloads = await self.payload_codec.decode(stored_payloads)

        return stored_payloads[0]

    async def retrieve_payloads(self, payloads: Payloads):
        stored_payloads = await self.retrieve_payload_sequence(payloads.payloads)
        for i, payload in enumerate(stored_payloads):
            payloads.payloads[i].CopyFrom(payload)

    async def retrieve_payload_sequence(
        self,
        payloads: Sequence[Payload],
    ) -> list[Payload]:
        results = list(payloads)

        if len(self.drivers) == 0:
            if any(len(p.external_payloads) > 0 for p in payloads):
                warnings.warn(
                    "ExternalStorage.drivers is empty, but detected external storage references.",
                    category=StorageWarning,
                )
            return results

        if len(payloads) == 1:
            return [await self.retrieve_payload(payloads[0])]

        driver_claims: dict[StorageDriver, list[tuple[int, StorageDriverClaim]]] = {}
        for index, payload in enumerate(payloads):
            if len(payload.external_payloads) == 0:
                continue

            reference = self._claim_converter.from_payload(payload, _StorageReference)
            if not isinstance(reference, _StorageReference):
                continue

            driver = self._get_driver_by_name(reference.driver_name)
            driver_claims.setdefault(driver, []).append((index, reference.driver_claim))

        if not driver_claims:
            return results

        context = StorageDriverContext(serialization_context=self._context)
        stored_by_index: dict[int, Payload] = {}

        driver_claim_list = list(driver_claims.items())

        all_stored = await asyncio.gather(
            *(
                driver.retrieve(context, [claim for _, claim in indexed_claims])
                for driver, indexed_claims in driver_claim_list
            )
        )

        for (driver, indexed_claims), stored_payloads in zip(
            driver_claim_list, all_stored
        ):
            indices = [idx for idx, _ in indexed_claims]

            self._validate_payload_length(
                stored_payloads,
                expected=len(indexed_claims),
                driver=driver,
            )

            for idx, stored_payload in zip(indices, stored_payloads):
                stored_by_index[idx] = stored_payload

        retrieve_indices = sorted(stored_by_index.keys())
        stored_list = [stored_by_index[idx] for idx in retrieve_indices]

        decoded_payloads = stored_list
        if self.payload_codec:
            decoded_payloads = await self.payload_codec.decode(stored_list)

        for i, retrieved_payload in enumerate(decoded_payloads):
            results[retrieve_indices[i]] = retrieved_payload

        return results

    def _validate_claim_length(
        self, claims: Sequence[StorageDriverClaim], expected: int, driver: StorageDriver
    ) -> None:
        if len(claims) != expected:
            raise ValueError(
                f"Driver '{driver.name()}' returned {len(claims)} claims, expected {expected}",
            )

    def _validate_payload_length(
        self, payloads: Sequence[Payload], expected: int, driver: StorageDriver
    ) -> None:
        if len(payloads) != expected:
            raise ValueError(
                f"Driver '{driver.name()}' returned {len(payloads)} payloads, expected {expected}",
            )
