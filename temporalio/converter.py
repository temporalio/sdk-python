"""Base converter and default implementations for conversion to/from values/payloads."""

import dataclasses
import inspect
import json
from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple, Type

import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database

import temporalio.api.common.v1


class DataConverter(ABC):
    """Base converter to/from multiple payloads/values."""

    @abstractmethod
    async def encode(self, values: list[Any]) -> list[temporalio.api.common.v1.Payload]:
        """Encode values into payloads.

        Args:
            values: Values to be converted.

        Returns:
            Converted payloads. Note, this does not have to be the same number
            as values given, but at least one must be present.

        Raises:
            Exception: Any issue during conversion.
        """
        raise NotImplementedError

    @abstractmethod
    async def decode(
        self,
        payloads: list[temporalio.api.common.v1.Payload],
        type_hints: Optional[list[Type]],
    ) -> list[Any]:
        """Decode payloads into values.

        Args:
            payloads: Payloads to convert to Python values.
            type_hints: Types that are expected if any. This may not have any
                types if there are no annotations on the target. If this is
                present, it must have the exact same length as payloads even if
                the values are just "object".

        Return:
            Collection of Python values. Note, this does not have to be the same
            number as values given, but at least one must be present.

        Raises:
            Exception: Any issue during conversion.
        """
        raise NotImplementedError


class PayloadConverter(ABC):
    """Base converter to/from single payload/value."""

    @abstractmethod
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """Encode a single value to a payload or None.

        Args:
            value: Value to be converted.

        Returns:
            Payload of the value or None if unable to convert.
        """
        return None

    @abstractmethod
    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        """Decode a single payload to a Python value if able.

        Args:
            payload: Payload to convert to Python value.
            type_hints: Type that is expected if any. This may not have a type
                if there are no annotations on the target.

        Return:
            A tuple with the first value as the Python value and the second as
            whether it could be converted or not. If the payload can be
            converted, it is the first value of the tuple and the second value
            is True. If the payload cannot be converted, the first value is
            undefined and the second value is False.
        """
        return (None, False)


class CompositeDataConverter(DataConverter):
    """Composite data converter that delegates to a list of payload converters.

    Encoding/decoding are attempted on each payload converter successively until
    it succeeds.

    Attributes:
        converters: List of payload converters to delegate to, in order.
    """

    converters: list[PayloadConverter]

    def __init__(self, *converters: PayloadConverter) -> None:
        """Initializes the data converter.

        Args:
            converters: Payload converters to delegate to, in order.
        """
        self.converters = list(converters)

    async def encode(self, values: list[Any]) -> list[temporalio.api.common.v1.Payload]:
        """Encode values trying each converter.

        See base class. Always returns the same number of payloads as values.
        """
        payloads = []
        for index, value in enumerate(values):
            # We intentionally attempt these serially just in case a stateful
            # converter may rely on the previous values
            payload = None
            for converter in self.converters:
                payload = await converter.encode(value)
                if payload is not None:
                    break
            if payload is None:
                raise RuntimeError(
                    f"Value at index {index} of type {type(value)} could not be converted"
                )
            payloads.append(payload)
        return payloads

    async def decode(
        self,
        payloads: list[temporalio.api.common.v1.Payload],
        type_hints: Optional[list[Type]],
    ) -> list[Any]:
        """Decode values trying each converter.

        See base class. Always returns the same number of values as payloads.
        """
        values = []
        for index, payload in enumerate(payloads):
            type_hint = None
            if type_hints is not None:
                type_hint = type_hints[index]
            # We intentionally attempt these serially just in case a stateful
            # converter may rely on the previous values
            ok = False
            for converter in self.converters:
                value, ok = await converter.decode(payload, type_hint)
                if ok:
                    break
            if not ok:
                encoding = payload.metadata.get("encoding", b"<unknown>").decode()
                raise RuntimeError(
                    f"Payload at index {index} with encoding '{encoding}' could not be converted"
                )
            values.append(value)
        return values


class BinaryNullPayloadConverter(PayloadConverter):
    """Converter for 'binary/null' payloads supporting None values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if value is None:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/null"}
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        return (None, payload.metadata["encoding"] == b"binary/null")


class BinaryPlainPayloadConverter(PayloadConverter):
    """Converter for 'binary/plain' payloads supporting bytes values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, bytes):
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/plain"}, data=value
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        """See base class."""
        return (payload.data, payload.metadata["encoding"] == b"binary/plain")


_sym_db = google.protobuf.symbol_database.Default()


class JSONProtoPayloadConverter(PayloadConverter):
    """Converter for 'json/protobuf' payloads supporting protobuf Message values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, google.protobuf.message.Message):
            # We have to convert to dict then to JSON because MessageToJson does
            # not have a compact option removing spaces and newlines
            json_str = json.dumps(
                google.protobuf.json_format.MessageToDict(value),
                separators=(",", ":"),
                sort_keys=True,
            )
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": b"json/protobuf",
                    "messageType": value.DESCRIPTOR.full_name.encode(),
                },
                data=json_str.encode(),
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == b"json/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(payload.metadata["messageType"].decode())()
            google.protobuf.json_format.Parse(payload.data, value)
            return (value, True)
        return (None, False)


class BinaryProtoPayloadConverter(PayloadConverter):
    """Converter for 'binary/protobuf' payloads supporting protobuf Message values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, google.protobuf.message.Message):
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": b"binary/protobuf",
                    "messageType": value.DESCRIPTOR.full_name.encode(),
                },
                data=value.SerializeToString(),
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == b"binary/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(payload.metadata["messageType"].decode())()
            value.ParseFromString(payload.data)
            return (value, True)
        return (None, False)


class JSONPlainPayloadConverter(PayloadConverter):
    """Converter for 'json/plain' payloads supporting common Python values.

    This supports all values that :py:func:`json.dump` supports and also adds
    encoding support for :py:mod:`dataclasses` by converting them using
    :py:func:`dataclasses.asdict`. Note that on decode, if there is a type hint,
    it will be used to construct the data class.
    """

    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _encoding: bytes

    def __init__(
        self,
        *,
        encoder: Optional[Type[json.JSONEncoder]] = None,
        decoder: Optional[Type[json.JSONDecoder]] = None,
        encoding: str = "json/plain",
    ) -> None:
        """Initialize a JSON data converter.

        Args:
            encoder: Custom encoder class object to use.
            decoder: Custom decoder class object to use.
            encoding: Encoding name to use.
        """
        super().__init__()
        self._encoder = encoder
        self._decoder = decoder
        self._encoding = encoding.encode()

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        # We swallow JSON encode error and just return None
        try:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self._encoding},
                data=json.dumps(
                    value, cls=self._encoder, separators=(",", ":"), sort_keys=True
                ).encode(),
            )
        except (RuntimeError, TypeError, ValueError):
            return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == self._encoding:
            # We do not swallow JSON decode errors since we expect success due
            # to already-matched encoding
            obj = json.loads(payload.data, cls=self._decoder)
            # If the object is a dict and the type hint is present for a data
            # class, we instantiate the data class with the value
            if (
                isinstance(obj, dict)
                and inspect.isclass(type_hint)
                and dataclasses.is_dataclass(type_hint)
            ):
                obj = type_hint(**obj)
            return (obj, True)
        return (None, False)


# TODO(cretz): Should this be a var that can be changed instead? If so, can it
# be replaced _after_ client creation? We'd just have to fallback to this
# default at conversion time instead of instantiation time.
def default() -> CompositeDataConverter:
    """Default converter compatible with other Temporal SDKs.

    This handles None, bytes, all protobuf message types, and any type that
    :py:func:`json.dump` accepts. In addition, this supports encoding
    :py:mod:`dataclasses` and also decoding them provided the data class is in
    the type hint.
    """
    return CompositeDataConverter(
        BinaryNullPayloadConverter(),
        BinaryPlainPayloadConverter(),
        JSONProtoPayloadConverter(),
        BinaryProtoPayloadConverter(),
        JSONPlainPayloadConverter(),
    )
