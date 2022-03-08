"""Base converter and default implementations for conversion to/from values/payloads."""

import dataclasses
import inspect
import json
import sys
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple, Type

import dacite
import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database

import temporalio.api.common.v1


class DataConverter(ABC):
    """Base converter to/from multiple payloads/values."""

    @abstractmethod
    async def encode(
        self, values: Iterable[Any]
    ) -> List[temporalio.api.common.v1.Payload]:
        """Encode values into payloads.

        Args:
            values: Values to be converted.

        Returns:
            Converted payloads. Note, this does not have to be the same number
            as values given, but must be at least one and cannot be more than
            was given.

        Raises:
            Exception: Any issue during conversion.
        """
        raise NotImplementedError

    @abstractmethod
    async def decode(
        self,
        payloads: Iterable[temporalio.api.common.v1.Payload],
        type_hints: Optional[List[Type]] = None,
    ) -> List[Any]:
        """Decode payloads into values.

        Args:
            payloads: Payloads to convert to Python values.
            type_hints: Types that are expected if any. This may not have any
                types if there are no annotations on the target. If this is
                present, it must have the exact same length as payloads even if
                the values are just "object".

        Returns:
            Collection of Python values. Note, this does not have to be the same
            number as values given, but at least one must be present.

        Raises:
            Exception: Any issue during conversion.
        """
        raise NotImplementedError


class PayloadConverter(ABC):
    """Base converter to/from single payload/value."""

    @property
    @abstractmethod
    def encoding(self) -> str:
        """Encoding for the payload this converter works with."""
        raise NotImplementedError

    @abstractmethod
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """Encode a single value to a payload or None.

        Args:
            value: Value to be converted.

        Returns:
            Payload of the value or None if unable to convert.

        Raises:
            TypeError: Value is not the expected type.
            ValueError: Value is of the expected type but otherwise incorrect.
            RuntimeError: General error during encoding.
        """
        raise NotImplementedError

    @abstractmethod
    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """Decode a single payload to a Python value or raise exception.

        Args:
            payload: Payload to convert to Python value.
            type_hints: Type that is expected if any. This may not have a type
                if there are no annotations on the target.

        Return:
            The decoded value from the payload. Since the encoding is checked by
            the caller, this should raise an exception if the payload cannot be
            converted.

        Raises:
            RuntimeError: General error during decoding.
        """
        raise NotImplementedError


class CompositeDataConverter(DataConverter):
    """Composite data converter that delegates to a list of payload converters.

    Encoding/decoding are attempted on each payload converter successively until
    it succeeds.

    Attributes:
        converters: List of payload converters to delegate to, in order.
    """

    converters: Mapping[bytes, PayloadConverter]

    def __init__(self, *converters: PayloadConverter) -> None:
        """Initializes the data converter.

        Args:
            converters: Payload converters to delegate to, in order.
        """
        # Insertion order preserved here
        self.converters = {c.encoding.encode(): c for c in converters}

    async def encode(
        self, values: Iterable[Any]
    ) -> List[temporalio.api.common.v1.Payload]:
        """Encode values trying each converter.

        See base class. Always returns the same number of payloads as values.

        Raises:
            RuntimeError: No known converter
        """
        payloads = []
        for index, value in enumerate(values):
            # We intentionally attempt these serially just in case a stateful
            # converter may rely on the previous values
            payload = None
            for converter in self.converters.values():
                payload = await converter.encode(value)
                if payload is not None:
                    break
            if payload is None:
                raise RuntimeError(
                    f"Value at index {index} of type {type(value)} has no known converter"
                )
            payloads.append(payload)
        return payloads

    async def decode(
        self,
        payloads: Iterable[temporalio.api.common.v1.Payload],
        type_hints: Optional[List[Type]] = None,
    ) -> List[Any]:
        """Decode values trying each converter.

        See base class. Always returns the same number of values as payloads.

        Raises:
            KeyError: Unknown payload encoding
            RuntimeError: Error during decode
        """
        values = []
        for index, payload in enumerate(payloads):
            encoding = payload.metadata.get("encoding", b"<unknown>")
            converter = self.converters.get(encoding)
            if converter is None:
                raise KeyError(f"Unknown payload encoding {encoding.decode()}")
            type_hint = None
            if type_hints is not None:
                type_hint = type_hints[index]
            try:
                values.append(await converter.decode(payload, type_hint))
            except RuntimeError as err:
                raise RuntimeError(
                    f"Payload at index {index} with encoding {encoding.decode()} could not be converted"
                ) from err
        return values


class BinaryNullPayloadConverter(PayloadConverter):
    """Converter for 'binary/null' payloads supporting None values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/null"

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if value is None:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self.encoding.encode()}
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        if len(payload.data) > 0:
            raise RuntimeError("Expected empty data set for binary/null")
        return None


class BinaryPlainPayloadConverter(PayloadConverter):
    """Converter for 'binary/plain' payloads supporting bytes values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/plain"

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, bytes):
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self.encoding.encode()}, data=value
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        return payload.data


_sym_db = google.protobuf.symbol_database.Default()


class JSONProtoPayloadConverter(PayloadConverter):
    """Converter for 'json/protobuf' payloads supporting protobuf Message values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "json/protobuf"

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if (
            isinstance(value, google.protobuf.message.Message)
            and value.DESCRIPTOR is not None
        ):
            # We have to convert to dict then to JSON because MessageToJson does
            # not have a compact option removing spaces and newlines
            json_str = json.dumps(
                google.protobuf.json_format.MessageToDict(value),
                separators=(",", ":"),
                sort_keys=True,
            )
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": self.encoding.encode(),
                    "messageType": value.DESCRIPTOR.full_name.encode(),
                },
                data=json_str.encode(),
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        message_type = payload.metadata.get("messageType", b"<unknown>").decode()
        try:
            value = _sym_db.GetSymbol(message_type)()
            return google.protobuf.json_format.Parse(payload.data, value)
        except KeyError as err:
            raise RuntimeError(f"Unknown Protobuf type {message_type}") from err
        except google.protobuf.json_format.ParseError as err:
            raise RuntimeError("Failed parsing") from err


class BinaryProtoPayloadConverter(PayloadConverter):
    """Converter for 'binary/protobuf' payloads supporting protobuf Message values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/protobuf"

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if (
            isinstance(value, google.protobuf.message.Message)
            and value.DESCRIPTOR is not None
        ):
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": self.encoding.encode(),
                    "messageType": value.DESCRIPTOR.full_name.encode(),
                },
                data=value.SerializeToString(),
            )
        return None

    async def decode(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        message_type = payload.metadata.get("messageType", b"<unknown>").decode()
        try:
            value = _sym_db.GetSymbol(message_type)()
            value.ParseFromString(payload.data)
            return value
        except KeyError as err:
            raise RuntimeError(f"Unknown Protobuf type {message_type}") from err
        except google.protobuf.message.DecodeError as err:
            raise RuntimeError("Failed parsing") from err


class JSONPlainPayloadConverter(PayloadConverter):
    """Converter for 'json/plain' payloads supporting common Python values.

    This supports all values that :py:func:`json.dump` supports and also adds
    encoding support for :py:mod:`dataclasses` by converting them using
    :py:mod:`dataclasses.asdict`. Note that on decode, if there is a type hint,
    it will be used to construct the data class.
    """

    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _encoding: str

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
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        """See base class."""
        return self._encoding

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        # We swallow JSON encode error and just return None
        try:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self._encoding.encode()},
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
    ) -> Any:
        """See base class."""
        try:
            obj = json.loads(payload.data, cls=self._decoder)
            # If the object is a dict and the type hint is present for a data
            # class, we instantiate the data class with the value
            if (
                isinstance(obj, dict)
                and inspect.isclass(type_hint)
                and dataclasses.is_dataclass(type_hint)
            ):
                # We have to use dacite here to handle nested dataclasses
                obj = dacite.from_dict(type_hint, obj)
            return obj
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed parsing") from err


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


async def encode_payloads(
    values: Iterable[Any], converter: DataConverter
) -> temporalio.api.common.v1.Payloads:
    """Encode :py:class:`temporalio.api.common.v1.Payloads` with the given
    converter.
    """
    return temporalio.api.common.v1.Payloads(payloads=await converter.encode(values))


async def decode_payloads(
    payloads: Optional[temporalio.api.common.v1.Payloads], converter: DataConverter
) -> List[Any]:
    """Decode :py:class:`temporalio.api.common.v1.Payloads` with the given
    converter.
    """
    if not payloads or not payloads.payloads:
        return []
    return await converter.decode(payloads.payloads)


def _type_hints_from_func(
    func: Callable[..., Any], *, eval_str: bool
) -> Tuple[Optional[List[Type]], Optional[Type]]:
    """Extracts the type hints from the function.

    Args:
        func: Function to extract hints from.
        eval_str: Whether to use ``eval_str`` (only supported on Python >= 3.10)

    Returns:
        Tuple containing parameter types and return type. The parameter types
        will be None if there are any non-positional parameters or if any of the
        parameters to not have an annotation that represents a class.
    """
    # eval_str only supported on >= 3.10
    if sys.version_info >= (3, 10):
        sig = inspect.signature(func, eval_str=eval_str)
    else:
        sig = inspect.signature(func)
    ret: Optional[Type] = None
    if inspect.isclass(sig.return_annotation):
        ret = sig.return_annotation
    args: List[Type] = []
    for value in sig.parameters.values():
        if (
            value.kind is not inspect.Parameter.POSITIONAL_ONLY
            and value.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
        ):
            return (None, ret)
        if not inspect.isclass(value.annotation):
            return (None, ret)
        args.append(value.annotation)
    return args, ret
