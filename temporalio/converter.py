"""Base converter and implementations for data conversion."""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import inspect
import json
import sys
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_type_hints,
)

import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database
from typing_extensions import Literal

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.common
import temporalio.exceptions

if sys.version_info < (3, 11):
    # Python's datetime.fromisoformat doesn't support certain formats pre-3.11
    from dateutil import parser  # type: ignore
# StrEnum is available in 3.11+
if sys.version_info >= (3, 11):
    from enum import StrEnum

if sys.version_info >= (3, 10):
    from types import UnionType


class PayloadConverter(ABC):
    """Base payload converter to/from multiple payloads/values."""

    default: ClassVar[PayloadConverter]
    """Default payload converter."""

    @abstractmethod
    def to_payloads(
        self, values: Sequence[Any]
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
        payloads: Sequence[temporalio.api.common.v1.Payload],
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
        self, values: Sequence[Any]
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
            type_hint: Type that is expected if any. This may not have a type if
                there are no annotations on the target.

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
        self, values: Sequence[Any]
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
        payloads: Sequence[temporalio.api.common.v1.Payload],
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
    :py:func:`json.dump` accepts. A singleton instance of this is available at
    :py:attr:`PayloadConverter.default`.
    """

    default_encoding_payload_converters: Tuple[EncodingPayloadConverter, ...]
    """Default set of encoding payload converters the default payload converter
    uses.
    """

    def __init__(self) -> None:
        """Create a default payload converter."""
        super().__init__(*DefaultPayloadConverter.default_encoding_payload_converters)


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


class AdvancedJSONEncoder(json.JSONEncoder):
    """Advanced JSON encoder.

    This encoder supports dataclasses, classes with dict() functions, and
    all iterables as lists.
    """

    def default(self, o: Any) -> Any:
        """Override JSON encoding default.

        See :py:meth:`json.JSONEncoder.default`.
        """
        # Dataclass support
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        # Support for models with "dict" function like Pydantic
        dict_fn = getattr(o, "dict", None)
        if callable(dict_fn):
            return dict_fn()
        # Support for non-list iterables like set
        if not isinstance(o, list) and isinstance(o, collections.abc.Iterable):
            return list(o)
        # Support for UUID
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


class JSONPlainPayloadConverter(EncodingPayloadConverter):
    """Converter for 'json/plain' payloads supporting common Python values.

    For encoding, this supports all values that :py:func:`json.dump` supports
    and by default adds extra encoding support for dataclasses, classes with
    ``dict()`` methods, and all iterables.

    For decoding, this uses type hints to attempt to rebuild the type from the
    type hint.
    """

    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _encoding: str

    def __init__(
        self,
        *,
        encoder: Optional[Type[json.JSONEncoder]] = AdvancedJSONEncoder,
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

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class."""
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
            if type_hint:
                obj = value_to_type(type_hint, obj)
            return obj
        except json.JSONDecodeError as err:
            raise RuntimeError("Failed parsing") from err


class PayloadCodec(ABC):
    """Codec for encoding/decoding to/from bytes.

    Commonly used for compression or encryption.
    """

    @abstractmethod
    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
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
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
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

    async def encode_failure(self, failure: temporalio.api.failure.v1.Failure) -> None:
        """Encode payloads of a failure."""
        await self._apply_to_failure_payloads(failure, self.encode_wrapper)

    async def decode_failure(self, failure: temporalio.api.failure.v1.Failure) -> None:
        """Decode payloads of a failure."""
        await self._apply_to_failure_payloads(failure, self.decode_wrapper)

    async def _apply_to_failure_payloads(
        self,
        failure: temporalio.api.failure.v1.Failure,
        cb: Callable[[temporalio.api.common.v1.Payloads], Awaitable[None]],
    ) -> None:
        if failure.HasField("encoded_attributes"):
            # Wrap in payloads and merge back
            payloads = temporalio.api.common.v1.Payloads(
                payloads=[failure.encoded_attributes]
            )
            await cb(payloads)
            failure.encoded_attributes.CopyFrom(payloads.payloads[0])
        if failure.HasField(
            "application_failure_info"
        ) and failure.application_failure_info.HasField("details"):
            await cb(failure.application_failure_info.details)
        elif failure.HasField(
            "timeout_failure_info"
        ) and failure.timeout_failure_info.HasField("last_heartbeat_details"):
            await cb(failure.timeout_failure_info.last_heartbeat_details)
        elif failure.HasField(
            "canceled_failure_info"
        ) and failure.canceled_failure_info.HasField("details"):
            await cb(failure.canceled_failure_info.details)
        elif failure.HasField(
            "reset_workflow_failure_info"
        ) and failure.reset_workflow_failure_info.HasField("last_heartbeat_details"):
            await cb(failure.reset_workflow_failure_info.last_heartbeat_details)
        if failure.HasField("cause"):
            await self._apply_to_failure_payloads(failure.cause, cb)


class FailureConverter(ABC):
    """Base failure converter to/from errors.

    Note, for workflow exceptions, :py:attr:`to_failure` is only invoked if the
    exception is an instance of :py:class:`temporalio.exceptions.FailureError`.
    Users should extend :py:class:`temporalio.exceptions.ApplicationError` if
    they want a custom workflow exception to work with this class.
    """

    default: ClassVar[FailureConverter]
    """Default failure converter."""

    @abstractmethod
    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        """Convert the given exception to a Temporal failure.

        Users should make sure not to alter the ``exception`` input.

        Args:
            exception: The exception to convert.
            payload_converter: The payload converter to use if needed.
            failure: The failure to update with error information.
        """
        raise NotImplementedError

    @abstractmethod
    def from_failure(
        self,
        failure: temporalio.api.failure.v1.Failure,
        payload_converter: PayloadConverter,
    ) -> BaseException:
        """Convert the given Temporal failure to an exception.

        Users should make sure not to alter the ``failure`` input.

        Args:
            failure: The failure to convert.
            payload_converter: The payload converter to use if needed.

        Returns:
            Converted error.
        """
        raise NotImplementedError


class DefaultFailureConverter(FailureConverter):
    """Default failure converter.

    A singleton instance of this is available at
    :py:attr:`FailureConverter.default`.
    """

    def __init__(self, *, encode_common_attributes: bool = False) -> None:
        """Create the default failure converter.

        Args:
            encode_common_attributes: If ``True``, the message and stack trace
                of the failure will be moved into the encoded attribute section
                of the failure which can be encoded with a codec.
        """
        super().__init__()
        self._encode_common_attributes = encode_common_attributes

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        """See base class."""
        # If already a failure error, use that
        if isinstance(exception, temporalio.exceptions.FailureError):
            self._error_to_failure(exception, payload_converter, failure)
        else:
            # Convert to failure error
            failure_error = temporalio.exceptions.ApplicationError(
                str(exception), type=exception.__class__.__name__
            )
            failure_error.__traceback__ = exception.__traceback__
            failure_error.__cause__ = exception.__cause__
            self._error_to_failure(failure_error, payload_converter, failure)
        # Encode common attributes if requested
        if self._encode_common_attributes:
            # Move message and stack trace to encoded attribute payload
            failure.encoded_attributes.CopyFrom(
                payload_converter.to_payloads(
                    [{"message": failure.message, "stack_trace": failure.stack_trace}]
                )[0]
            )
            failure.message = "Encoded failure"
            failure.stack_trace = ""

    def _error_to_failure(
        self,
        error: temporalio.exceptions.FailureError,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        # If there is an underlying proto already, just use that
        if error.failure:
            failure.CopyFrom(error.failure)
            return

        # Set message, stack, and cause. Obtaining cause follows rules from
        # https://docs.python.org/3/library/exceptions.html#exception-context
        failure.message = error.message
        if error.__traceback__:
            failure.stack_trace = "\n".join(traceback.format_tb(error.__traceback__))
        if error.__cause__:
            self.to_failure(error.__cause__, payload_converter, failure.cause)
        elif not error.__suppress_context__ and error.__context__:
            self.to_failure(error.__context__, payload_converter, failure.cause)

        # Set specific subclass values
        if isinstance(error, temporalio.exceptions.ApplicationError):
            failure.application_failure_info.SetInParent()
            if error.type:
                failure.application_failure_info.type = error.type
            failure.application_failure_info.non_retryable = error.non_retryable
            if error.details:
                failure.application_failure_info.details.CopyFrom(
                    payload_converter.to_payloads_wrapper(error.details)
                )
        elif isinstance(error, temporalio.exceptions.TimeoutError):
            failure.timeout_failure_info.SetInParent()
            failure.timeout_failure_info.timeout_type = (
                temporalio.api.enums.v1.TimeoutType.ValueType(error.type or 0)
            )
            if error.last_heartbeat_details:
                failure.timeout_failure_info.last_heartbeat_details.CopyFrom(
                    payload_converter.to_payloads_wrapper(error.last_heartbeat_details)
                )
        elif isinstance(error, temporalio.exceptions.CancelledError):
            failure.canceled_failure_info.SetInParent()
            if error.details:
                failure.canceled_failure_info.details.CopyFrom(
                    payload_converter.to_payloads_wrapper(error.details)
                )
        elif isinstance(error, temporalio.exceptions.TerminatedError):
            failure.terminated_failure_info.SetInParent()
        elif isinstance(error, temporalio.exceptions.ServerError):
            failure.server_failure_info.SetInParent()
            failure.server_failure_info.non_retryable = error.non_retryable
        elif isinstance(error, temporalio.exceptions.ActivityError):
            failure.activity_failure_info.SetInParent()
            failure.activity_failure_info.scheduled_event_id = error.scheduled_event_id
            failure.activity_failure_info.started_event_id = error.started_event_id
            failure.activity_failure_info.identity = error.identity
            failure.activity_failure_info.activity_type.name = error.activity_type
            failure.activity_failure_info.activity_id = error.activity_id
            failure.activity_failure_info.retry_state = (
                temporalio.api.enums.v1.RetryState.ValueType(error.retry_state or 0)
            )
        elif isinstance(error, temporalio.exceptions.ChildWorkflowError):
            failure.child_workflow_execution_failure_info.SetInParent()
            failure.child_workflow_execution_failure_info.namespace = error.namespace
            failure.child_workflow_execution_failure_info.workflow_execution.workflow_id = (
                error.workflow_id
            )
            failure.child_workflow_execution_failure_info.workflow_execution.run_id = (
                error.run_id
            )
            failure.child_workflow_execution_failure_info.workflow_type.name = (
                error.workflow_type
            )
            failure.child_workflow_execution_failure_info.initiated_event_id = (
                error.initiated_event_id
            )
            failure.child_workflow_execution_failure_info.started_event_id = (
                error.started_event_id
            )
            failure.child_workflow_execution_failure_info.retry_state = (
                temporalio.api.enums.v1.RetryState.ValueType(error.retry_state or 0)
            )

    def from_failure(
        self,
        failure: temporalio.api.failure.v1.Failure,
        payload_converter: PayloadConverter,
    ) -> BaseException:
        """See base class."""
        # If encoded attributes are present and have the fields we expect,
        # extract them
        if failure.HasField("encoded_attributes"):
            # Clone the failure to not mutate the incoming failure
            new_failure = temporalio.api.failure.v1.Failure()
            new_failure.CopyFrom(failure)
            failure = new_failure
            try:
                encoded_attributes: Dict[str, Any] = payload_converter.from_payloads(
                    [failure.encoded_attributes]
                )[0]
                if isinstance(encoded_attributes, dict):
                    message = encoded_attributes.get("message")
                    if isinstance(message, str):
                        failure.message = message
                    stack_trace = encoded_attributes.get("stack_trace")
                    if isinstance(stack_trace, str):
                        failure.stack_trace = stack_trace
            except:
                pass

        err: temporalio.exceptions.FailureError
        if failure.HasField("application_failure_info"):
            app_info = failure.application_failure_info
            err = temporalio.exceptions.ApplicationError(
                failure.message or "Application error",
                *payload_converter.from_payloads_wrapper(app_info.details),
                type=app_info.type or None,
                non_retryable=app_info.non_retryable,
            )
        elif failure.HasField("timeout_failure_info"):
            timeout_info = failure.timeout_failure_info
            err = temporalio.exceptions.TimeoutError(
                failure.message or "Timeout",
                type=temporalio.exceptions.TimeoutType(int(timeout_info.timeout_type))
                if timeout_info.timeout_type
                else None,
                last_heartbeat_details=payload_converter.from_payloads_wrapper(
                    timeout_info.last_heartbeat_details
                ),
            )
        elif failure.HasField("canceled_failure_info"):
            cancel_info = failure.canceled_failure_info
            err = temporalio.exceptions.CancelledError(
                failure.message or "Cancelled",
                *payload_converter.from_payloads_wrapper(cancel_info.details),
            )
        elif failure.HasField("terminated_failure_info"):
            err = temporalio.exceptions.TerminatedError(failure.message or "Terminated")
        elif failure.HasField("server_failure_info"):
            server_info = failure.server_failure_info
            err = temporalio.exceptions.ServerError(
                failure.message or "Server error",
                non_retryable=server_info.non_retryable,
            )
        elif failure.HasField("activity_failure_info"):
            act_info = failure.activity_failure_info
            err = temporalio.exceptions.ActivityError(
                failure.message or "Activity error",
                scheduled_event_id=act_info.scheduled_event_id,
                started_event_id=act_info.started_event_id,
                identity=act_info.identity,
                activity_type=act_info.activity_type.name,
                activity_id=act_info.activity_id,
                retry_state=temporalio.exceptions.RetryState(int(act_info.retry_state))
                if act_info.retry_state
                else None,
            )
        elif failure.HasField("child_workflow_execution_failure_info"):
            child_info = failure.child_workflow_execution_failure_info
            err = temporalio.exceptions.ChildWorkflowError(
                failure.message or "Child workflow error",
                namespace=child_info.namespace,
                workflow_id=child_info.workflow_execution.workflow_id,
                run_id=child_info.workflow_execution.run_id,
                workflow_type=child_info.workflow_type.name,
                initiated_event_id=child_info.initiated_event_id,
                started_event_id=child_info.started_event_id,
                retry_state=temporalio.exceptions.RetryState(
                    int(child_info.retry_state)
                )
                if child_info.retry_state
                else None,
            )
        else:
            err = temporalio.exceptions.FailureError(failure.message or "Failure error")
        err._failure = failure
        if failure.HasField("cause"):
            err.__cause__ = self.from_failure(failure.cause, payload_converter)
        return err


class DefaultFailureConverterWithEncodedAttributes(DefaultFailureConverter):
    """Implementation of :py:class:`DefaultFailureConverter` which moves message
    and stack trace to encoded attributes subject to a codec.
    """

    def __init__(self) -> None:
        """Create a default failure converter with encoded attributes."""
        super().__init__(encode_common_attributes=True)


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

    failure_converter_class: Type[FailureConverter] = DefaultFailureConverter
    """Class to instantiate for failure conversion."""

    payload_converter: PayloadConverter = dataclasses.field(init=False)
    """Payload converter created from the :py:attr:`payload_converter_class`."""

    failure_converter: FailureConverter = dataclasses.field(init=False)
    """Failure converter created from the :py:attr:`failure_converter_class`."""

    default: ClassVar[DataConverter]
    """Singleton default data converter."""

    def __post_init__(self) -> None:  # noqa: D105
        object.__setattr__(self, "payload_converter", self.payload_converter_class())
        object.__setattr__(self, "failure_converter", self.failure_converter_class())

    async def encode(
        self, values: Sequence[Any]
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
        payloads: Sequence[temporalio.api.common.v1.Payload],
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
        self, values: Sequence[Any]
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

    async def encode_failure(
        self, exception: BaseException, failure: temporalio.api.failure.v1.Failure
    ) -> None:
        """Convert and encode failure."""
        self.failure_converter.to_failure(exception, self.payload_converter, failure)
        if self.payload_codec:
            await self.payload_codec.encode_failure(failure)

    async def decode_failure(
        self, failure: temporalio.api.failure.v1.Failure
    ) -> BaseException:
        """Decode and convert failure."""
        if self.payload_codec:
            await self.payload_codec.decode_failure(failure)
        return self.failure_converter.from_failure(failure, self.payload_converter)


DefaultPayloadConverter.default_encoding_payload_converters = (
    BinaryNullPayloadConverter(),
    BinaryPlainPayloadConverter(),
    JSONProtoPayloadConverter(),
    BinaryProtoPayloadConverter(),
    JSONPlainPayloadConverter(),
)

DataConverter.default = DataConverter()

PayloadConverter.default = DataConverter.default.payload_converter

FailureConverter.default = DataConverter.default.failure_converter


def default() -> DataConverter:
    """Default data converter.

    .. deprecated::
        Use :py:meth:`DataConverter.default` instead.
    """
    return DataConverter.default


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


def _get_iso_datetime_parser() -> Callable[[str], datetime]:
    """Isolates system version check and returns relevant datetime passer

    Returns:
        A callable to parse date strings into datetimes.
    """
    if sys.version_info >= (3, 11):
        return datetime.fromisoformat  # noqa
    else:
        # Isolate import for py > 3.11, as dependency only installed for < 3.11
        return parser.isoparse


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
            parser = _get_iso_datetime_parser()
            val = [parser(v) for v in val]
        ret[k] = val
    return ret


def value_to_type(hint: Type, value: Any) -> Any:
    """Convert a given value to the given type hint.

    This is used internally to convert a raw JSON loaded value to a specific
    type hint.

    Args:
        hint: Type hint to convert the value to.
        value: Raw value (e.g. primitive, dict, or list) to convert from.

    Returns:
        Converted value.

    Raises:
        TypeError: Unable to convert to the given hint.
    """
    # Any or primitives
    if hint is Any:
        return value
    elif hint is int or hint is float:
        if not isinstance(value, (int, float)):
            raise TypeError(f"Expected value to be int|float, was {type(value)}")
        return hint(value)
    elif hint is bool:
        if not isinstance(value, bool):
            raise TypeError(f"Expected value to be bool, was {type(value)}")
        return bool(value)
    elif hint is str:
        if not isinstance(value, str):
            raise TypeError(f"Expected value to be str, was {type(value)}")
        return str(value)
    elif hint is bytes:
        if not isinstance(value, (str, bytes, list)):
            raise TypeError(f"Expected value to be bytes, was {type(value)}")
        # In some other SDKs, this is serialized as a base64 string, but in
        # Python this is a numeric array.
        return bytes(value)  # type: ignore
    elif hint is type(None):
        if value is not None:
            raise TypeError(f"Expected None, got value of type {type(value)}")
        return None

    # NewType. Note we cannot simply check isinstance NewType here because it's
    # only been a class since 3.10. Instead we'll just check for the presence
    # of a supertype.
    supertype = getattr(hint, "__supertype__", None)
    if supertype:
        return value_to_type(supertype, value)

    # Load origin for other checks
    origin = getattr(hint, "__origin__", hint)
    type_args: Tuple = getattr(hint, "__args__", ())

    # Literal
    if origin is Literal:
        if value not in type_args:
            raise TypeError(f"Value {value} not in literal values {type_args}")
        return value

    is_union = origin is Union
    if sys.version_info >= (3, 10):
        is_union = is_union or isinstance(origin, UnionType)

    # Union
    if is_union:
        # Try each one. Note, Optional is just a union w/ none.
        for arg in type_args:
            try:
                return value_to_type(arg, value)
            except Exception:
                pass
        raise TypeError(f"Failed converting to {hint} from {value}")

    # Mapping
    if inspect.isclass(origin) and issubclass(origin, collections.abc.Mapping):
        if not isinstance(value, collections.abc.Mapping):
            raise TypeError(f"Expected {hint}, value was {type(value)}")
        ret_dict = {}
        # If there are required or optional keys that means we are a TypedDict
        # and therefore can extract per-key types
        per_key_types: Optional[Dict[str, Type]] = None
        if getattr(origin, "__required_keys__", None) or getattr(
            origin, "__optional_keys__", None
        ):
            per_key_types = get_type_hints(origin)
        key_type = (
            type_args[0]
            if len(type_args) > 0
            and type_args[0] is not Any
            and not isinstance(type_args[0], TypeVar)
            else None
        )
        value_type = (
            type_args[1]
            if len(type_args) > 1
            and type_args[1] is not Any
            and not isinstance(type_args[1], TypeVar)
            else None
        )
        # Convert each key/value
        for key, value in value.items():
            if key_type:
                try:
                    key = value_to_type(key_type, key)
                except Exception as err:
                    raise TypeError(f"Failed converting key {key} on {hint}") from err
            # If there are per-key types, use it instead of single type
            this_value_type = value_type
            if per_key_types:
                # TODO(cretz): Strict mode would fail an unknown key
                this_value_type = per_key_types.get(key)
            if this_value_type:
                try:
                    value = value_to_type(this_value_type, value)
                except Exception as err:
                    raise TypeError(
                        f"Failed converting value for key {key} on {hint}"
                    ) from err
            ret_dict[key] = value
        # If there are per-key types, it's a typed dict and we want to attempt
        # instantiation to get its validation
        if per_key_types:
            ret_dict = hint(**ret_dict)
        return ret_dict

    # Dataclass
    if dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise TypeError(
                f"Cannot convert to dataclass {hint}, value is {type(value)} not dict"
            )
        # Obtain dataclass fields and check that all dict fields are there and
        # that no required fields are missing. Unknown fields are silently
        # ignored.
        fields = dataclasses.fields(hint)
        field_hints = get_type_hints(hint)
        field_values = {}
        for field in fields:
            field_value = value.get(field.name, dataclasses.MISSING)
            # We do not check whether field is required here. Rather, we let the
            # attempted instantiation of the dataclass raise if a field is
            # missing
            if field_value is not dataclasses.MISSING:
                try:
                    field_values[field.name] = value_to_type(
                        field_hints[field.name], field_value
                    )
                except Exception as err:
                    raise TypeError(
                        f"Failed converting field {field.name} on dataclass {hint}"
                    ) from err
        # Simply instantiate the dataclass. This will fail as expected when
        # missing required fields.
        # TODO(cretz): Want way to convert snake case to camel case?
        return hint(**field_values)

    # If there is a @staticmethod or @classmethod parse_obj, we will use it.
    # This covers Pydantic models.
    parse_obj_attr = inspect.getattr_static(hint, "parse_obj", None)
    if isinstance(parse_obj_attr, classmethod) or isinstance(
        parse_obj_attr, staticmethod
    ):
        if not isinstance(value, dict):
            raise TypeError(
                f"Cannot convert to {hint}, value is {type(value)} not dict"
            )
        return getattr(hint, "parse_obj")(value)

    # IntEnum
    if inspect.isclass(hint) and issubclass(hint, IntEnum):
        if not isinstance(value, int):
            raise TypeError(
                f"Cannot convert to enum {hint}, value not an integer, value is {type(value)}"
            )
        return hint(value)

    # StrEnum, available in 3.11+
    if sys.version_info >= (3, 11):
        if inspect.isclass(hint) and issubclass(hint, StrEnum):
            if not isinstance(value, str):
                raise TypeError(
                    f"Cannot convert to enum {hint}, value not a string, value is {type(value)}"
                )
            return hint(value)

    # UUID
    if inspect.isclass(hint) and issubclass(hint, uuid.UUID):
        return hint(value)

    # Iterable. We intentionally put this last as it catches several others.
    if inspect.isclass(origin) and issubclass(origin, collections.abc.Iterable):
        if not isinstance(value, collections.abc.Iterable):
            raise TypeError(f"Expected {hint}, value was {type(value)}")
        ret_list = []
        # If there is no type arg, just return value as is
        if not type_args or (
            len(type_args) == 1
            and (isinstance(type_args[0], TypeVar) or type_args[0] is Ellipsis)
        ):
            ret_list = list(value)
        else:
            # Otherwise convert
            for i, item in enumerate(value):
                # Non-tuples use first type arg, tuples use arg set or one
                # before ellipsis if that's set
                if origin is not tuple:
                    arg_type = type_args[0]
                elif len(type_args) > i and type_args[i] is not Ellipsis:
                    arg_type = type_args[i]
                elif type_args[-1] is Ellipsis:
                    # Ellipsis means use the second to last one
                    arg_type = type_args[-2]
                else:
                    raise TypeError(
                        f"Type {hint} only expecting {len(type_args)} values, got at least {i + 1}"
                    )
                try:
                    ret_list.append(value_to_type(arg_type, item))
                except Exception as err:
                    raise TypeError(f"Failed converting {hint} index {i}") from err
        # If tuple, set, or deque convert back to that type
        if origin is tuple:
            return tuple(ret_list)
        elif origin is set:
            return set(ret_list)
        elif origin is collections.deque:
            return collections.deque(ret_list)
        return ret_list

    raise TypeError(f"Unserializable type during conversion: {hint}")
