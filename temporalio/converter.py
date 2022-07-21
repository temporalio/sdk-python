"""Base converter and implementations for data conversion."""

from __future__ import annotations

import dataclasses
import inspect
import json
import types
import typing
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Type

import dacite
import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database

import temporalio.api.common.v1
import temporalio.common


class PayloadConverter(ABC):
    """Base payload converter to/from multiple payloads/values."""

    @abstractmethod
    def to_payloads(
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
    def from_payloads(
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

    def to_payloads_wrapper(
        self, values: Iterable[Any]
    ) -> temporalio.api.common.v1.Payloads:
        """:py:meth:`to_payloads` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.
        """
        return temporalio.api.common.v1.Payloads(payloads=self.to_payloads(values))

    def from_payloads_wrapper(
        self, payloads: Optional[temporalio.api.common.v1.Payloads]
    ) -> List[Any]:
        """:py:meth:`from_payloads` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.
        """
        if not payloads or not payloads.payloads:
            return []
        return self.from_payloads(payloads.payloads)


class EncodingPayloadConverter(ABC):
    """Base converter to/from single payload/value with a known encoding for use in CompositePayloadConverter."""

    @property
    @abstractmethod
    def encoding(self) -> str:
        """Encoding for the payload this converter works with."""
        raise NotImplementedError

    @abstractmethod
    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
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
    def from_payload(
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


class CompositePayloadConverter(PayloadConverter):
    """Composite payload converter that delegates to a list of encoding payload converters.

    Encoding/decoding are attempted on each payload converter successively until
    it succeeds.

    Attributes:
        converters: List of payload converters to delegate to, in order.
    """

    converters: Mapping[bytes, EncodingPayloadConverter]

    def __init__(self, *converters: EncodingPayloadConverter) -> None:
        """Initializes the data converter.

        Args:
            converters: Payload converters to delegate to, in order.
        """
        # Insertion order preserved here since Python 3.7
        self.converters = {c.encoding.encode(): c for c in converters}

    def to_payloads(
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
                payload = converter.to_payload(value)
                if payload is not None:
                    break
            if payload is None:
                raise RuntimeError(
                    f"Value at index {index} of type {type(value)} has no known converter"
                )
            payloads.append(payload)
        return payloads

    def from_payloads(
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
                values.append(converter.from_payload(payload, type_hint))
            except RuntimeError as err:
                raise RuntimeError(
                    f"Payload at index {index} with encoding {encoding.decode()} could not be converted"
                ) from err
        return values


class DefaultPayloadConverter(CompositePayloadConverter):
    """Default payload converter compatible with other Temporal SDKs.

    This handles None, bytes, all protobuf message types, and any type that
    :py:func:`json.dump` accepts. In addition, this supports encoding
    :py:mod:`dataclasses` and also decoding them provided the data class is in
    the type hint.
    """

    def __init__(self) -> None:
        """Create a default payload converter."""
        super().__init__(
            BinaryNullPayloadConverter(),
            BinaryPlainPayloadConverter(),
            JSONProtoPayloadConverter(),
            BinaryProtoPayloadConverter(),
            JSONPlainPayloadConverter(),
        )


class BinaryNullPayloadConverter(EncodingPayloadConverter):
    """Converter for 'binary/null' payloads supporting None values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/null"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if value is None:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self.encoding.encode()}
            )
        return None

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        if len(payload.data) > 0:
            raise RuntimeError("Expected empty data set for binary/null")
        return None


class BinaryPlainPayloadConverter(EncodingPayloadConverter):
    """Converter for 'binary/plain' payloads supporting bytes values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/plain"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if isinstance(value, bytes):
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self.encoding.encode()}, data=value
            )
        return None

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        return payload.data


_sym_db = google.protobuf.symbol_database.Default()


class JSONProtoPayloadConverter(EncodingPayloadConverter):
    """Converter for 'json/protobuf' payloads supporting protobuf Message values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "json/protobuf"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
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

    def from_payload(
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


class BinaryProtoPayloadConverter(EncodingPayloadConverter):
    """Converter for 'binary/protobuf' payloads supporting protobuf Message values."""

    @property
    def encoding(self) -> str:
        """See base class."""
        return "binary/protobuf"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
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

    def from_payload(
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


class JSONPlainPayloadConverter(EncodingPayloadConverter):
    """Converter for 'json/plain' payloads supporting common Python values.

    This supports all values that :py:func:`json.dump` supports and also adds
    encoding support for :py:mod:`dataclasses` by converting them using
    :py:mod:`dataclasses.asdict`. Note that on decode, if there is a type hint,
    it will be used to construct the data class.
    """

    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _encoding: str
    _dacite_config: dacite.Config

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
        self._dacite_config = dacite.Config(cast=[IntEnum])

    @property
    def encoding(self) -> str:
        """See base class."""
        return self._encoding

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
        if dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        # We let JSON conversion errors be thrown to caller
        return temporalio.api.common.v1.Payload(
            metadata={"encoding": self._encoding.encode()},
            data=json.dumps(
                value, cls=self._encoder, separators=(",", ":"), sort_keys=True
            ).encode(),
        )

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """See base class."""
        try:
            obj = json.loads(payload.data, cls=self._decoder)

            # If the object is an int and the type hint is an IntEnum, convert
            if isinstance(obj, int) and type_hint and issubclass(type_hint, IntEnum):
                obj = type_hint(obj)

            # If the object is a dict and the type hint is present for a data
            # class, we instantiate the data class with the value
            if (
                isinstance(obj, dict)
                and inspect.isclass(type_hint)
                and dataclasses.is_dataclass(type_hint)
            ):
                # We have to use dacite here to handle nested dataclasses
                obj = dacite.from_dict(type_hint, obj, self._dacite_config)

            return obj
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed parsing") from err


class PayloadCodec(ABC):
    """Codec for encoding/decoding to/from bytes.

    Commonly used for compression or encryption.
    """

    @abstractmethod
    async def encode(
        self, payloads: Iterable[temporalio.api.common.v1.Payload]
    ) -> List[temporalio.api.common.v1.Payload]:
        """Encode the given payloads.

        Args:
            payloads: Payloads to encode. This value should not be mutated.

        Returns:
            Encoded payloads. Note, this does not have to be the same number as
            payloads given, but must be at least one and cannot be more than was
            given.
        """
        raise NotImplementedError

    @abstractmethod
    async def decode(
        self, payloads: Iterable[temporalio.api.common.v1.Payload]
    ) -> List[temporalio.api.common.v1.Payload]:
        """Decode the given payloads.

        Args:
            payloads: Payloads to decode. This value should not be mutated.

        Returns:
            Decoded payloads. Note, this does not have to be the same number as
            payloads given, but must be at least one and cannot be more than was
            given.
        """
        raise NotImplementedError

    async def encode_wrapper(self, payloads: temporalio.api.common.v1.Payloads) -> None:
        """:py:meth:`encode` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.

        This replaces the payloads within the wrapper.
        """
        new_payloads = await self.encode(payloads.payloads)
        del payloads.payloads[:]
        # TODO(cretz): Copy too expensive?
        payloads.payloads.extend(new_payloads)

    async def decode_wrapper(self, payloads: temporalio.api.common.v1.Payloads) -> None:
        """:py:meth:`decode` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.

        This replaces the payloads within.
        """
        new_payloads = await self.decode(payloads.payloads)
        del payloads.payloads[:]
        # TODO(cretz): Copy too expensive?
        payloads.payloads.extend(new_payloads)


@dataclass(frozen=True)
class DataConverter:
    """Data converter for converting and encoding payloads to/from Python values.

    This combines :py:class:`PayloadConverter` which converts values with
    :py:class:`PayloadCodec` which encodes bytes.
    """

    payload_converter_class: Type[PayloadConverter] = DefaultPayloadConverter
    """Class to instantiate for payload conversion."""

    payload_codec: Optional[PayloadCodec] = None
    """Optional codec for encoding payload bytes."""

    payload_converter: PayloadConverter = dataclasses.field(init=False)

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "payload_converter", self.payload_converter_class())

    async def encode(
        self, values: Iterable[Any]
    ) -> List[temporalio.api.common.v1.Payload]:
        """Encode values into payloads.

        First converts values to payloads then encodes payloads using codec.

        Args:
            values: Values to be converted and encoded.

        Returns:
            Converted and encoded payloads. Note, this does not have to be the
            same number as values given, but must be at least one and cannot be
            more than was given.
        """
        payloads = self.payload_converter.to_payloads(values)
        if self.payload_codec:
            payloads = await self.payload_codec.encode(payloads)
        return payloads

    async def decode(
        self,
        payloads: Iterable[temporalio.api.common.v1.Payload],
        type_hints: Optional[List[Type]] = None,
    ) -> List[Any]:
        """Decode payloads into values.

        First decodes payloads using codec then converts payloads to values.

        Args:
            payloads: Payloads to be decoded and converted.

        Returns:
            Decoded and converted values.
        """
        if self.payload_codec:
            payloads = await self.payload_codec.decode(payloads)
        return self.payload_converter.from_payloads(payloads, type_hints)

    async def encode_wrapper(
        self, values: Iterable[Any]
    ) -> temporalio.api.common.v1.Payloads:
        """:py:meth:`encode` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.
        """
        return temporalio.api.common.v1.Payloads(payloads=(await self.encode(values)))

    async def decode_wrapper(
        self,
        payloads: Optional[temporalio.api.common.v1.Payloads],
        type_hints: Optional[List[Type]] = None,
    ) -> List[Any]:
        """:py:meth:`decode` for the
        :py:class:`temporalio.api.common.v1.Payloads` wrapper.
        """
        if not payloads or not payloads.payloads:
            return []
        return await self.decode(payloads.payloads, type_hints)


_default: Optional[DataConverter] = None


def default() -> DataConverter:
    """Default data converter."""
    global _default
    if not _default:
        _default = DataConverter()
    return _default


def encode_search_attributes(
    attributes: temporalio.common.SearchAttributes,
    api: temporalio.api.common.v1.SearchAttributes,
) -> None:
    """Convert search attributes into an API message.

    Args:
        attributes: Search attributes to convert.
        api: API message to set converted attributes on.
    """
    for k, v in attributes.items():
        api.indexed_fields[k].CopyFrom(encode_search_attribute_values(v))


def encode_search_attribute_values(
    vals: temporalio.common.SearchAttributeValues,
) -> temporalio.api.common.v1.Payload:
    """Convert search attribute values into a payload.

    Args:
        vals: List of values to convert.
    """
    if not isinstance(vals, list):
        raise TypeError("Search attribute values must be lists")
    # Confirm all types are the same
    val_type: Optional[Type] = None
    # Convert dates to strings
    safe_vals = []
    for v in vals:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                raise ValueError(
                    "Timezone must be present on all search attribute dates"
                )
            v = v.isoformat()
        elif not isinstance(v, (str, int, float, bool)):
            raise TypeError(
                f"Search attribute value of type {type(v).__name__} not one of str, int, float, bool, or datetime"
            )
        elif val_type and type(v) is not val_type:
            raise TypeError(
                f"Search attribute values must have the same type for the same key"
            )
        elif not val_type:
            val_type = type(v)
        safe_vals.append(v)
    return default().payload_converter.to_payloads([safe_vals])[0]


def decode_search_attributes(
    api: temporalio.api.common.v1.SearchAttributes,
) -> temporalio.common.SearchAttributes:
    """Decode API search attributes to values.

    Args:
        api: API message with search attribute values to convert.

    Returns:
        Converted search attribute values.
    """
    conv = default().payload_converter
    ret = {}
    for k, v in api.indexed_fields.items():
        val = conv.from_payloads([v])[0]
        # If a value did not come back as a list, make it a single-item list
        if not isinstance(val, list):
            val = [val]
        # Convert each item to datetime if necessary
        if v.metadata.get("type") == b"Datetime":
            val = [datetime.fromisoformat(v) for v in val]
        ret[k] = val
    return ret


class _FunctionTypeLookup:
    def __init__(self) -> None:
        # Keyed by callable __qualname__, value is optional arg types and
        # optional ret type
        self._cache: Dict[str, Tuple[Optional[List[Type]], Optional[Type]]] = {}

    def get_type_hints(self, fn: Any) -> Tuple[Optional[List[Type]], Optional[Type]]:
        # Due to MyPy issues, we cannot type "fn" as callable
        if not callable(fn):
            return (None, None)
        # We base the cache key on the qualified name of the function. However,
        # since some callables are not functions, we assume we can never cache
        # these just in case the type hints are dynamic for some strange reason.
        cache_key = getattr(fn, "__qualname__", None)
        if cache_key:
            ret = self._cache.get(cache_key)
            if ret:
                return ret
        # TODO(cretz): Do we even need to cache?
        ret = _type_hints_from_func(fn)
        if cache_key:
            self._cache[cache_key] = ret
        return ret


# Same as inspect._NonUserDefinedCallables
_non_user_defined_callables = (
    type(type.__call__),
    type(all.__call__),  # type: ignore
    type(int.__dict__["from_bytes"]),
    types.BuiltinFunctionType,
)


def _type_hints_from_func(
    func: Callable,
) -> Tuple[Optional[List[Type]], Optional[Type]]:
    """Extracts the type hints from the function.

    Args:
        func: Function to extract hints from.

    Returns:
        Tuple containing parameter types and return type. The parameter types
        will be None if there are any non-positional parameters or if any of the
        parameters to not have an annotation that represents a class. If the
        first parameter is "self" with no attribute, it is not included.
    """
    # If this is a class instance with user-defined __call__, then use that as
    # the func. This mimics inspect logic inside Python.
    if (
        not inspect.isfunction(func)
        and not isinstance(func, type)
        and not isinstance(func, _non_user_defined_callables)
        and not isinstance(func, types.MethodType)
    ):
        # Callable instance
        call_func = getattr(type(func), "__call__", None)
        if call_func is not None and not isinstance(
            type(func), _non_user_defined_callables
        ):
            func = call_func
    # We use inspect.signature for the parameter names and kinds, but we cannot
    # use it for annotations because those that are using deferred hinting (i.e.
    # from __future__ import annotations) only work with the eval_str parameter
    # which is only supported in >= 3.10. But typing.get_type_hints is supported
    # in >= 3.7.
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func)
    ret_hint = hints.get("return")
    ret = (
        ret_hint
        if inspect.isclass(ret_hint) and ret_hint is not inspect.Signature.empty
        else None
    )
    args: List[Type] = []
    for index, value in enumerate(sig.parameters.values()):
        # Ignore self on methods
        if (
            index == 0
            and value.name == "self"
            and value.annotation is inspect.Parameter.empty
        ):
            continue
        # Stop if non-positional or not a class
        if (
            value.kind is not inspect.Parameter.POSITIONAL_ONLY
            and value.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
        ):
            return (None, ret)
        # All params must have annotations or we consider none to have them
        arg_hint = hints.get(value.name)
        if not inspect.isclass(arg_hint) or arg_hint is inspect.Parameter.empty:
            return (None, ret)
        args.append(arg_hint)
    return args, ret
