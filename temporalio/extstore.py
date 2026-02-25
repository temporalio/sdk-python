"""External payload storage support for offloading payloads to external storage systems."""

from __future__ import annotations

import asyncio
import dataclasses
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import Self

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    JSONPlainPayloadConverter,
    SerializationContext,
    WithSerializationContext,
)
from temporalio.exceptions import TemporalError

if TYPE_CHECKING:
    from temporalio.converter import PayloadCodec


@dataclass(frozen=True)
class DriverClaim:
    """Claim for an externally stored payload.

    .. warning::
           This API is experimental.
    """

    data: dict[str, str]
    """Driver-defined data for identifying and retrieving an externally stored payload."""


@dataclass(frozen=True)
class DriverContext:
    """Context passed to :class:`Driver` and :class:`DriverSelector` calls.

    .. warning::
           This API is experimental.
    """

    serialization_context: SerializationContext | None = None
    """The serialization context active when this driver operation was initiated,
    or ``None`` if no context has been set.
    """


class Driver(ABC):
    """Base driver for storing and retrieve payloads from external storage systems.

    .. warning::
           This API is experimental.
    """

    @abstractmethod
    def name(self) -> str:
        """Returns the name of this driver instance. A driver may allow its name
        to be parameterized at construction time so that multiple instances of
        the same driver class can coexist in :attr:`StorageOptions.drivers` with
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
        context: DriverContext,
        payloads: Sequence[Payload],
    ) -> list[DriverClaim]:
        """Stores payloads in external storage and returns a :class:`DriverClaim`
        for each one. The returned list must be the same length as ``payloads``.
        """
        raise NotImplementedError

    @abstractmethod
    async def retrieve(
        self,
        context: DriverContext,
        claims: Sequence[DriverClaim],
    ) -> list[Payload]:
        """Retrieves payloads from external storage for the given :class:`DriverClaim`
        list. The returned list must be the same length as ``claims``.

        Raise :class:`PayloadNotFoundError` when a retrieval attempt confirms
        that a payload is absent from storage. This signals an unrecoverable
        condition that will fail the workflow rather than retrying the workflow
        task.
        """
        raise NotImplementedError


class DriverSelector(ABC):
    """Determines which :class:`Driver` stores a given payload.

    Implement this class and set it as :attr:`StorageOptions.driver_selector` when you
    need stateful or class-based selection logic. For simple cases a plain
    callable ``(DriverContext, Payload) -> Driver | None`` can be used instead.

    .. warning::
           This API is experimental.
    """

    @abstractmethod
    def select_driver(self, context: DriverContext, payload: Payload) -> Driver | None:
        """Returns the driver to use to externally store the payload, or None to decline to
        externally store the payload.
        """
        pass


@dataclass(frozen=True)
class StorageConverter(WithSerializationContext):
    """Converters for converting and encoding external payloads to/from Python values.

    .. warning::
           This API is experimental.
    """

    payload_codec: PayloadCodec | None
    """Optional codec applied to payloads before they are handed to a
    :class:`Driver` for storage, and after they are retrieved. When ``None``,
    payloads are stored as-is by the driver.
    """

    def with_context(self, context: SerializationContext) -> Self:
        """Return a copy of this converter with the serialization context applied.

        If :attr:`payload_codec` implements :class:`WithSerializationContext`,
        a new instance is created with the context propagated to it. If nothing
        changed, ``self`` is returned unchanged.
        """
        payload_codec = self.payload_codec
        if isinstance(payload_codec, WithSerializationContext):
            payload_codec = payload_codec.with_context(context)
        if payload_codec == self.payload_codec:
            return self
        cloned = dataclasses.replace(self)
        object.__setattr__(cloned, "payload_codec", payload_codec)
        return cloned


@dataclass(frozen=True)
class StorageOptions(WithSerializationContext):
    """Configuration for external storage behavior.

    .. warning::
           This API is experimental.
    """

    drivers: Sequence[Driver]
    """Drivers available for storing and retrieving payloads. At least one
    driver must be provided.

    When no :attr:`driver_selector` is set, the first driver in this list is
    used for all store operations. Additional drivers may be included solely to
    support retrieval — for example, to download payloads that remote callers
    uploaded to an external storage system that is not your primary store
    driver. Drivers in this list are looked up by :meth:`Driver.name` during
    retrieval, so each driver must have a unique name.
    """

    driver_selector: (
        DriverSelector | Callable[[DriverContext, Payload], Driver | None] | None
    ) = None
    """Controls which driver stores a given payload. Accepts either a
    :class:`DriverSelector` instance or a callable of the form
    ``(DriverContext, Payload) -> Driver | None``.

    When ``None``, the first driver in :attr:`drivers` is used for all store
    operations. Returning ``None`` from the selector leaves the payload stored
    inline rather than offloading it to external storage.
    """

    payload_size_threshold: int | None = 256 * 1024
    """Minimum payload size in bytes before external storage is considered.
    Defaults to 256 KiB. Set to ``None`` to consider every payload for
    external storage regardless of size.
    """

    external_converter: StorageConverter | None = None
    """Converter applied to payload bytes before they are passed to a driver
    for storage, and after they are retrieved. When ``None``, payload bytes are
    handed to the driver without any additional encoding. Note that the
    ``DataConverter``'s ``payload_codec`` is applied to the reference payload
    that replaces the original in workflow history, not to the externally stored
    bytes themselves.
    """

    def with_context(self, context: SerializationContext) -> Self:
        """Return a copy of these options with the serialization context applied.

        Propagates *context* to any drivers, the driver selector, and the
        external converter that implement :class:`WithSerializationContext`.
        If none of those fields changed, ``self`` is returned unchanged.
        """
        drivers = list(self.drivers)
        for index, driver in enumerate(drivers):
            if isinstance(driver, WithSerializationContext):
                drivers[index] = driver.with_context(context)
        driver_selector = self.driver_selector
        if isinstance(driver_selector, WithSerializationContext):
            driver_selector = driver_selector.with_context(context)
        external_converter = self.external_converter
        if isinstance(external_converter, WithSerializationContext):
            external_converter = external_converter.with_context(context)
        if all(
            new is orig
            for new, orig in [
                (drivers, self.drivers),
                (driver_selector, self.driver_selector),
                (external_converter, self.external_converter),
            ]
        ):
            return self
        cloned = dataclasses.replace(self)
        object.__setattr__(cloned, "drivers", drivers)
        object.__setattr__(cloned, "driver_selector", driver_selector)
        object.__setattr__(cloned, "external_converter", external_converter)
        return cloned


class DriverError(TemporalError):
    """Raised when an error occurs related to a specific driver.

    .. warning::
            This API is experimental.
    """

    def __init__(self, message: str, driver_name: str) -> None:
        """Initialize with an error message and the name of the driver that failed."""
        super().__init__(message)
        self._driver_name = driver_name

    @property
    def driver_name(self) -> str:
        """Name of the driver that caused this error."""
        return self._driver_name


class DriverNotFoundError(DriverError):
    """Raised when a driver name cannot be resolved to a driver in
    :attr:`StorageOptions.drivers`. This can occur during retrieval when a
    :class:`DriverClaim` references a driver name that is not present, or
    during storage when the :attr:`StorageOptions.driver_selector` returns a
    :class:`Driver` whose :meth:`Driver.name` is not registered.

    .. warning::
           This API is experimental.
    """

    def __init__(self, driver_name: str) -> None:
        """Initialize with the name of the driver that could not be resolved."""
        super().__init__(
            f"No driver found with name '{driver_name}'", driver_name=driver_name
        )


class PayloadNotFoundError(TemporalError):
    """Raised when a payload cannot be retrieved because it does not exist
    at the location indicated by its :class:`DriverClaim`.

    When raised during workflow execution this error fails the **workflow**
    rather than the workflow task. Drivers should raise this when a retrieval
    attempt confirms the payload is absent.

    This error is intentionally not a subclass of :class:`DriverError` to
    avoid accidentally handling it and treating as a workflow task failure.

    .. warning::
           This API is experimental.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        driver_claim: DriverClaim,
        driver_name: str,
    ) -> None:
        """Initialize a payload not found error."""
        super().__init__(message or f"Payload not found for driver '{driver_name}'")
        self._driver_claim = driver_claim
        self._driver_name = driver_name

    @property
    def driver_claim(self) -> DriverClaim:
        """The :class:`DriverClaim` for the payload that could not be found."""
        return self._driver_claim

    @property
    def driver_name(self) -> str:
        """Name of the driver that reported the payload as not found."""
        return self._driver_name


class StorageWarning(RuntimeWarning):
    """Warning for external storage issues.

    .. warning::
           This API is experimental.
    """


@dataclass(frozen=True)
class _StorageReference:
    driver_name: str
    driver_claim: DriverClaim


class _ExternalStorageMiddleware:  # type:ignore[reportUnusedClass]
    # Claim payload encoding is fixed and independent of any user configuration.
    _claim_converter: JSONPlainPayloadConverter = JSONPlainPayloadConverter(
        encoding="json/external-storage-reference"
    )

    def __init__(
        self,
        options: StorageOptions | None,
        context: SerializationContext | None = None,
        payload_codec: PayloadCodec | None = None,
    ):
        self._options = options
        self._context = context
        self._payload_codec = (
            options.external_converter.payload_codec
            if options and options.external_converter
            else payload_codec
        )
        self._driver_map: dict[str, Driver] = {}
        if options is not None:
            for driver in options.drivers:
                name = driver.name()
                if name in self._driver_map:
                    warnings.warn(
                        f"StorageOptions.drivers contains multiple drivers with name '{name}'. "
                        "The first one will be used.",
                        category=StorageWarning,
                    )
                else:
                    self._driver_map[name] = driver

    def _select_driver(self, context: DriverContext, payload: Payload) -> Driver | None:
        """Returns the driver to use for this payload, or None to pass through."""
        assert self._options is not None
        selector = self._options.driver_selector
        if selector is None:
            return self._options.drivers[0] if self._options.drivers else None
        elif isinstance(selector, DriverSelector):
            driver = selector.select_driver(context, payload)
        else:
            driver = selector(context, payload)
        if driver is None:
            return None
        registered = self._driver_map.get(driver.name())
        if registered is None:
            raise DriverNotFoundError(driver.name())
        return registered

    def _get_driver_by_name(self, name: str) -> Driver:
        """Looks up a driver by name, raising :class:`DriverNotFoundError` if not found."""
        driver = self._driver_map.get(name)
        if driver is None:
            raise DriverNotFoundError(name)
        return driver

    async def store_payload(self, payload: Payload) -> Payload:
        if self._options is None:
            return payload

        size_bytes = payload.ByteSize()
        if (
            self._options.payload_size_threshold is not None
            and size_bytes < self._options.payload_size_threshold
        ):
            return payload

        context = DriverContext(serialization_context=self._context)

        driver = self._select_driver(context, payload)
        if driver is None:
            return payload

        # Optionally encode the payload before externally storing it
        encoded_payload = payload
        if self._payload_codec:
            encoded_payload = (await self._payload_codec.encode([payload]))[0]

        try:
            claims = await driver.store(context, [encoded_payload])
        except Exception as err:
            raise DriverError("Driver store failed", driver.name()) from err

        self._validate_claim_length(claims, expected=1, driver=driver)

        reference = _StorageReference(
            driver_name=driver.name(),
            driver_claim=claims[0],
        )
        reference_payload = self._claim_converter.to_payload(reference)
        assert reference_payload is not None
        reference_payload.external_payloads.add().size_bytes = (
            encoded_payload.ByteSize()
        )
        return reference_payload

    async def store_payloads(
        self,
        payloads: Sequence[Payload],
    ) -> list[Payload]:
        if self._options is None:
            return list(payloads)

        if len(payloads) == 1:
            return [await self.store_payload(payloads[0])]

        results = list(payloads)
        context = DriverContext(serialization_context=self._context)

        # First pass: determine which payloads to store and which driver to use for each.
        # Provide unencoded payloads to give maximal context information to the selector.
        to_store: list[tuple[int, Payload, Driver]] = []
        for index, payload in enumerate(payloads):
            size_bytes = payload.ByteSize()
            if (
                self._options.payload_size_threshold is not None
                and size_bytes < self._options.payload_size_threshold
            ):
                continue
            driver = self._select_driver(context, payload)
            if driver is None:
                continue
            to_store.append((index, payload, driver))

        if not to_store:
            return results

        # Optionally encode all payloads destined for external storage
        payloads_to_encode = [payload for _, payload, _ in to_store]
        encoded_payloads = payloads_to_encode
        if self._payload_codec:
            encoded_payloads = await self._payload_codec.encode(payloads_to_encode)

        # Group encoded payloads by driver for batched store calls
        # driver -> [(original_index, encoded_payload)]
        driver_groups: dict[Driver, list[tuple[int, Payload]]] = {}
        for i, (orig_index, _, driver) in enumerate(to_store):
            driver_groups.setdefault(driver, []).append(
                (orig_index, encoded_payloads[i])
            )

        # Store all driver groups concurrently then build reference payloads
        driver_group_list = list(driver_groups.items())

        async def _store_group(
            driver: Driver, indexed_payloads: list[tuple[int, Payload]]
        ) -> list[DriverClaim]:
            store_batch = [p for _, p in indexed_payloads]
            try:
                return await driver.store(context, store_batch)
            except Exception as err:
                raise DriverError("Driver store failed", driver.name()) from err

        all_claims = await asyncio.gather(
            *(
                _store_group(driver, indexed_payloads)
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
                assert reference_payload is not None
                reference_payload.external_payloads.add().size_bytes = sizes[i]
                results[indices[i]] = reference_payload

        return results

    async def retrieve_payload(
        self,
        payload: Payload,
    ) -> Payload:
        if self._options is None or len(self._options.drivers) == 0:
            # External storage was not configured (correctly). Warn if there are any external payloads
            # since that is likely to cause downstream error when decoding or deserializing.
            if len(payload.external_payloads) > 0:
                if not self._options:
                    warnings.warn(
                        "External storage is not configured, but detected external storage references.",
                        category=StorageWarning,
                    )
                elif len(self._options.drivers) == 0:
                    warnings.warn(
                        "StorageOptions.drivers is empty, but detected external storage references.",
                        category=StorageWarning,
                    )
            return payload

        if len(payload.external_payloads) == 0:
            return payload

        reference = self._claim_converter.from_payload(payload, _StorageReference)
        if not isinstance(reference, _StorageReference):
            return payload

        driver = self._get_driver_by_name(reference.driver_name)
        context = DriverContext(serialization_context=self._context)

        try:
            stored_payloads = await driver.retrieve(context, [reference.driver_claim])
        except PayloadNotFoundError:
            raise
        except Exception as err:
            raise DriverError("Driver retrieve failed", driver.name()) from err

        self._validate_payload_length(stored_payloads, expected=1, driver=driver)

        if self._payload_codec:
            stored_payloads = await self._payload_codec.decode(stored_payloads)

        return stored_payloads[0]

    async def retrieve_payloads(
        self,
        payloads: Sequence[Payload],
    ) -> list[Payload]:
        results = list(payloads)

        if self._options is None or len(self._options.drivers) == 0:
            # External storage was not configured, but warn if there are any external payloads
            # since that is likely to cause downstream error when decoding or deserializing.
            if any(len(p.external_payloads) > 0 for p in payloads):
                if not self._options:
                    warnings.warn(
                        "External storage is not configured, but detected external storage references.",
                        category=StorageWarning,
                    )
                elif len(self._options.drivers) == 0:
                    warnings.warn(
                        "StorageOptions.drivers is empty, but detected external storage references.",
                        category=StorageWarning,
                    )
            return results

        if len(payloads) == 1:
            return [await self.retrieve_payload(payloads[0])]

        # Group claims by driver for batched retrieve calls
        # driver -> [(original_index, claim)]
        driver_claims: dict[Driver, list[tuple[int, DriverClaim]]] = {}
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

        context = DriverContext(serialization_context=self._context)
        stored_by_index: dict[int, Payload] = {}

        # Retrieve from all drivers concurrently
        driver_claim_list = list(driver_claims.items())

        async def _retrieve_group(
            driver: Driver, indexed_claims: list[tuple[int, DriverClaim]]
        ) -> list[Payload]:
            claims_to_retrieve = [claim for _, claim in indexed_claims]
            try:
                return await driver.retrieve(context, claims_to_retrieve)
            except PayloadNotFoundError:
                raise
            except Exception as err:
                raise DriverError("Driver retrieve failed", driver.name()) from err

        all_stored = await asyncio.gather(
            *(
                _retrieve_group(driver, indexed_claims)
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

        # Decode all retrieved payloads together if a codec is configured
        retrieve_indices = sorted(stored_by_index.keys())
        stored_list = [stored_by_index[idx] for idx in retrieve_indices]

        decoded_payloads = stored_list
        if self._payload_codec:
            decoded_payloads = await self._payload_codec.decode(stored_list)

        for i, retrieved_payload in enumerate(decoded_payloads):
            results[retrieve_indices[i]] = retrieved_payload

        return results

    def _validate_claim_length(
        self, claims: Sequence[DriverClaim], expected: int, driver: Driver
    ) -> None:
        if len(claims) != expected:
            raise DriverError(
                f"Driver returned {len(claims)} claims, expected {expected}",
                driver.name(),
            )

    def _validate_payload_length(
        self, payloads: Sequence[Payload], expected: int, driver: Driver
    ) -> None:
        if len(payloads) != expected:
            raise DriverError(
                f"Driver returned {len(payloads)} payloads, expected {expected}",
                driver.name(),
            )
