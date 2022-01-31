"""Base converter and default implementations for conversion to/from values/payloads."""

import dataclasses
import json
from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple, Type

import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database

import temporalio.api.common.v1


class PayloadConverter(ABC):
    """Base converter to/from values/payloads."""

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
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """Decode a single payload to a Python value if able.

        Args:
            payload: Payload to convert to Python value.

        Return:
            A tuple with the first value as the Python value and the second as
            whether it could be converted or not. If the payload can be
            converted, it is the first value of the tuple and the second value
            is True. If the payload cannot be converted, the first value is
            undefined and the second value is False.
        """
        return (None, False)

    async def encode_multiple(
        self, values: list[Any]
    ) -> Optional[temporalio.api.common.v1.Payloads]:
        """Encode multiple values into payloads if able.

        Values are expected to be of a common payload type/encoding. The default
        implementation makes one payload for each value but subclasses may alter
        that.

        Args:
            values: List of values to convert.
        """
        payloads = []
        for value in values:
            payload = await self.encode(value)
            # Return if any payloads cannot be converted
            if payload is None:
                return None
            payloads.append(payload)
        return temporalio.api.common.v1.Payloads(payloads=payloads)

    async def decode_multiple(
        self, payloads: temporalio.api.common.v1.Payloads
    ) -> Tuple[list[Any], bool]:
        """Decode multiple payloads into Python values if able.

        Payloads are expected to be of a common type/encoding. The default
        implementation makes value for each payload but subclasses may alter
        that.

        Args:
            payloads: Payloads to convert to Python values.

        Return:
            A tuple with the first value as a collection of Python values and
            the second as whether it could be converted or not. If the payloads
            can be converted, they are the first values of the tuple and the
            second value is True. If the payloads cannot be converted, the first
            value is undefined and the second value is False.
        """
        values = []
        for payload in payloads.payloads:
            value, ok = await self.decode(payload)
            # Return if any values cannot be converted
            if not ok:
                return ([], False)
            values.append(value)
        return (values, True)


class CompositePayloadConverter(PayloadConverter):
    """Composite converter that delegates to a list of converters.

    Encoding/decoding are attempted on each converter successively until it
    succeeds.

    Attributes:
        converters: List of converters to delegate to, in order.
    """

    converters: list[PayloadConverter]

    def __init__(self, *converters: PayloadConverter) -> None:
        """Initializes the converter.

        Args:
            converters: Converters to delegate to, in order.
        """
        self.converters = list(converters)

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """Encode value trying each converter. See base class."""
        for converter in self.converters:
            payload = await converter.encode(value)
            if payload is not None:
                return payload
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """Decode payload trying each converter. See base class."""
        for converter in self.converters:
            value, ok = await converter.decode(payload)
            if ok:
                return (value, True)
        return (None, False)

    async def encode_multiple(
        self, values: list[Any]
    ) -> Optional[temporalio.api.common.v1.Payloads]:
        """Encode multiple values trying each converter. See base class.

        Note, this attempts full encode_multiple calls on the delegated
        converter. It does not allow different converters to encode different
        values. A single converter must be able to convert them all at once.
        """
        for converter in self.converters:
            payloads = await converter.encode_multiple(values)
            if payloads is not None:
                return payloads
        return None

    async def decode_multiple(
        self, payloads: temporalio.api.common.v1.Payloads
    ) -> Tuple[list[Any], bool]:
        """Decode multiple payloads trying each converter. See base class.

        Note, this attempts full decode_multiple calls on the delegated
        converter. It does not allow different converters to decode different
        payloads. A single converter must be able to convert them all at once.
        """
        for converter in self.converters:
            values, ok = await converter.decode_multiple(payloads)
            if ok:
                return (values, True)
        return ([], False)


class BinaryNullPayloadConverter(PayloadConverter):
    """Converter for binary/null payloads supporting None values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if value is None:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/null"}
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        return (None, payload.metadata["encoding"] == b"binary/null")


class BinaryPlainPayloadConverter(PayloadConverter):
    """Converter for binary/plain payloads supporting bytes values."""

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, bytes):
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/plain"}, data=value
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """See base class."""
        return (payload.data, payload.metadata["encoding"] == b"binary/plain")


_sym_db = google.protobuf.symbol_database.Default()


class JSONProtoPayloadConverter(PayloadConverter):
    """Converter for json/protobuf payloads supporting protobuf Message values."""

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
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == b"json/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(payload.metadata["messageType"].decode())()
            google.protobuf.json_format.Parse(payload.data, value)
            return (value, True)
        return (None, False)


class BinaryProtoPayloadConverter(PayloadConverter):
    """Converter for binary/protobuf payloads supporting protobuf Message values."""

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
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == b"binary/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(payload.metadata["messageType"].decode())()
            value.ParseFromString(payload.data)
            return (value, True)
        return (None, False)


class JSONPlainPayloadConverter(PayloadConverter):
    """Converter for json/plain payloads supporting common Python values.

    This supports all values that :py:func:`json.dump` supports and also adds
    encoding support for :py:mod:`dataclasses` by converting them using
    :py:func:`dataclasses.asdict`. Note that on decode they come back as dict as
    well and the caller must convert back to a data class.
    """

    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _dataclass_asdict: bool
    _encoding: bytes

    def __init__(
        self,
        *,
        encoder: Optional[Type[json.JSONEncoder]] = None,
        decoder: Optional[Type[json.JSONDecoder]] = None,
        dataclass_asdict: bool = True,
        encoding: str = "json/plain"
    ) -> None:
        """Initialize a JSON data converter.

        Args:
            encoder: Custom encoder class object to use.
            decoder: Custom decoder class object to use.
            dataclass_asdict: Whether to support data class encoding.
            encoding: Encoding name to use.
        """
        super().__init__()
        self._encoder = encoder
        self._decoder = decoder
        self._dataclass_asdict = dataclass_asdict
        self._encoding = encoding.encode()

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if self._dataclass_asdict and dataclasses.is_dataclass(value):
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
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        """See base class."""
        if payload.metadata["encoding"] == self._encoding:
            # We do not swallow JSON decode errors since we expect success due
            # to already-matched encoding
            return (json.loads(payload.data, cls=self._decoder), True)
        return (None, False)


# TODO(cretz): Should this be a var that can be changed instead? If so, can it
# be replaced _after_ client creation? We'd just have to fallback to this
# default at conversion time instead of instantiation time.
def default() -> CompositePayloadConverter:
    """Default converter compatible with other Temporal SDKs.

    This handles None, bytes, all protobuf message types, and any type that
    :py:func:`json.dump` accepts. In addition, this supports encoding
    :py:mod:`dataclasses` but not decoding them, so decoded data classes appear
    as dicts which may need to be converted to data classes by users.
    """
    return CompositePayloadConverter(
        BinaryNullPayloadConverter(),
        BinaryPlainPayloadConverter(),
        JSONProtoPayloadConverter(),
        BinaryProtoPayloadConverter(),
        JSONPlainPayloadConverter(),
    )
