"""Common code used in the Temporal SDK."""

from __future__ import annotations

import inspect
import types
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from typing import (
    Any,
    Callable,
    ClassVar,
    Collection,
    Generic,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Text,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_type_hints,
    overload,
)

import google.protobuf.internal.containers
from typing_extensions import ClassVar, NamedTuple, TypeAlias, get_origin

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.types


@dataclass
class RetryPolicy:
    """Options for retrying workflows and activities."""

    initial_interval: timedelta = timedelta(seconds=1)
    """Backoff interval for the first retry. Default 1s."""

    backoff_coefficient: float = 2.0
    """Coefficient to multiply previous backoff interval by to get new
    interval. Default 2.0.
    """

    maximum_interval: Optional[timedelta] = None
    """Maximum backoff interval between retries. Default 100x
    :py:attr:`initial_interval`.
    """

    maximum_attempts: int = 0
    """Maximum number of attempts.
    
    If 0, the default, there is no maximum.
    """

    non_retryable_error_types: Optional[Sequence[str]] = None
    """List of error types that are not retryable."""

    @staticmethod
    def from_proto(proto: temporalio.api.common.v1.RetryPolicy) -> RetryPolicy:
        """Create a retry policy from the proto object."""
        return RetryPolicy(
            initial_interval=proto.initial_interval.ToTimedelta(),
            backoff_coefficient=proto.backoff_coefficient,
            maximum_interval=proto.maximum_interval.ToTimedelta()
            if proto.HasField("maximum_interval")
            else None,
            maximum_attempts=proto.maximum_attempts,
            non_retryable_error_types=proto.non_retryable_error_types
            if proto.non_retryable_error_types
            else None,
        )

    def apply_to_proto(self, proto: temporalio.api.common.v1.RetryPolicy) -> None:
        """Apply the fields in this policy to the given proto object."""
        # Do validation before converting
        self._validate()
        # Convert
        proto.initial_interval.FromTimedelta(self.initial_interval)
        proto.backoff_coefficient = self.backoff_coefficient
        proto.maximum_interval.FromTimedelta(
            self.maximum_interval or self.initial_interval * 100
        )
        proto.maximum_attempts = self.maximum_attempts
        if self.non_retryable_error_types:
            proto.non_retryable_error_types.extend(self.non_retryable_error_types)

    def _validate(self) -> None:
        # Validation taken from Go SDK's test suite
        if self.maximum_attempts == 1:
            # Ignore other validation if disabling retries
            return
        if self.initial_interval.total_seconds() < 0:
            raise ValueError("Initial interval cannot be negative")
        if self.backoff_coefficient < 1:
            raise ValueError("Backoff coefficient cannot be less than 1")
        if self.maximum_interval:
            if self.maximum_interval.total_seconds() < 0:
                raise ValueError("Maximum interval cannot be negative")
            if self.maximum_interval < self.initial_interval:
                raise ValueError(
                    "Maximum interval cannot be less than initial interval"
                )
        if self.maximum_attempts < 0:
            raise ValueError("Maximum attempts cannot be negative")


class WorkflowIDReusePolicy(IntEnum):
    """How already-in-use workflow IDs are handled on start.

    See :py:class:`temporalio.api.enums.v1.WorkflowIdReusePolicy`.
    """

    ALLOW_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )
    ALLOW_DUPLICATE_FAILED_ONLY = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY
    )
    REJECT_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE
    )
    TERMINATE_IF_RUNNING = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_TERMINATE_IF_RUNNING
    )


class QueryRejectCondition(IntEnum):
    """Whether a query should be rejected in certain conditions.

    See :py:class:`temporalio.api.enums.v1.QueryRejectCondition`.
    """

    NONE = int(temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NONE)
    NOT_OPEN = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_OPEN
    )
    NOT_COMPLETED_CLEANLY = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_COMPLETED_CLEANLY
    )


@dataclass(frozen=True)
class RawValue:
    """Representation of an unconverted, raw payload.

    This type can be used as a parameter or return type in workflows,
    activities, signals, and queries to pass through a raw payload.
    Encoding/decoding of the payload is still done by the system.
    """

    payload: temporalio.api.common.v1.Payload

    def __getstate__(self) -> object:
        """Pickle support."""
        # We'll convert payload to bytes and prepend a version number just in
        # case we want to extend in the future
        return b"1" + self.payload.SerializeToString()

    def __setstate__(self, state: object) -> None:
        """Pickle support."""
        if not isinstance(state, bytes):
            raise TypeError(f"Expected bytes state, got {type(state)}")
        if not state[:1] == b"1":
            raise ValueError("Bad version prefix")
        object.__setattr__(
            self, "payload", temporalio.api.common.v1.Payload.FromString(state[1:])
        )


# We choose to make this a list instead of an sequence so we can catch if people
# are not sending lists each time but maybe accidentally sending a string (which
# is a sequence)
SearchAttributeValues: TypeAlias = Union[
    List[str], List[int], List[float], List[bool], List[datetime]
]

SearchAttributes: TypeAlias = Mapping[str, SearchAttributeValues]

SearchAttributeValue: TypeAlias = Union[str, int, float, bool, datetime, Sequence[str]]

SearchAttributeValueType = TypeVar(
    "SearchAttributeValueType", str, int, float, bool, datetime, Sequence[str]
)


class SearchAttributeIndexedValueType(IntEnum):
    """Server index type of a search attribute."""

    TEXT = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_TEXT)
    KEYWORD = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD)
    INT = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_INT)
    DOUBLE = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_DOUBLE)
    BOOL = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_BOOL)
    DATETIME = int(temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_DATETIME)
    KEYWORD_LIST = int(
        temporalio.api.enums.v1.IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD_LIST
    )


class SearchAttributeKey(ABC, Generic[SearchAttributeValueType]):
    """Typed search attribute key representation.

    Use one of the ``for`` static methods here to create a key.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the name of the key."""
        ...

    @property
    @abstractmethod
    def indexed_value_type(self) -> SearchAttributeIndexedValueType:
        """Get the server index typed of the key"""
        ...

    @property
    @abstractmethod
    def value_type(self) -> Type[SearchAttributeValueType]:
        """Get the Python type of value for the key.

        This may contain generics which cannot be used in ``isinstance``.
        :py:attr:`origin_value_type` can be used instead.
        """
        ...

    @property
    def origin_value_type(self) -> Type:
        """Get the Python type of value for the key without generics."""
        return get_origin(self.value_type) or self.value_type

    @property
    def _metadata_type(self) -> str:
        index_type = self.indexed_value_type
        if index_type == SearchAttributeIndexedValueType.TEXT:
            return "Text"
        elif index_type == SearchAttributeIndexedValueType.KEYWORD:
            return "Keyword"
        elif index_type == SearchAttributeIndexedValueType.INT:
            return "Int"
        elif index_type == SearchAttributeIndexedValueType.DOUBLE:
            return "Double"
        elif index_type == SearchAttributeIndexedValueType.BOOL:
            return "Bool"
        elif index_type == SearchAttributeIndexedValueType.DATETIME:
            return "Datetime"
        elif index_type == SearchAttributeIndexedValueType.KEYWORD_LIST:
            return "KeywordList"
        raise ValueError(f"Unrecognized type: {self}")

    def value_set(
        self, value: SearchAttributeValueType
    ) -> SearchAttributeUpdate[SearchAttributeValueType]:
        """Create a search attribute update to set the given value on this
        key.
        """
        return _SearchAttributeUpdate[SearchAttributeValueType](self, value)

    def value_unset(self) -> SearchAttributeUpdate[SearchAttributeValueType]:
        """Create a search attribute update to unset the value on this key."""
        return _SearchAttributeUpdate[SearchAttributeValueType](self, None)

    @staticmethod
    def for_text(name: str) -> SearchAttributeKey[str]:
        """Create a 'Text' search attribute type."""
        return _SearchAttributeKey[str](name, SearchAttributeIndexedValueType.TEXT, str)

    @staticmethod
    def for_keyword(name: str) -> SearchAttributeKey[str]:
        """Create a 'Keyword' search attribute type."""
        return _SearchAttributeKey[str](
            name, SearchAttributeIndexedValueType.KEYWORD, str
        )

    @staticmethod
    def for_int(name: str) -> SearchAttributeKey[int]:
        """Create an 'Int' search attribute type."""
        return _SearchAttributeKey[int](name, SearchAttributeIndexedValueType.INT, int)

    @staticmethod
    def for_float(name: str) -> SearchAttributeKey[float]:
        """Create a 'Double' search attribute type."""
        return _SearchAttributeKey[float](
            name, SearchAttributeIndexedValueType.DOUBLE, float
        )

    @staticmethod
    def for_bool(name: str) -> SearchAttributeKey[bool]:
        """Create a 'Bool' search attribute type."""
        return _SearchAttributeKey[bool](
            name, SearchAttributeIndexedValueType.BOOL, bool
        )

    @staticmethod
    def for_datetime(name: str) -> SearchAttributeKey[datetime]:
        """Create a 'Datetime' search attribute type."""
        return _SearchAttributeKey[datetime](
            name, SearchAttributeIndexedValueType.DATETIME, datetime
        )

    @staticmethod
    def for_keyword_list(name: str) -> SearchAttributeKey[Sequence[str]]:
        """Create a 'KeywordList' search attribute type."""
        return _SearchAttributeKey[Sequence[str]](
            name,
            SearchAttributeIndexedValueType.KEYWORD_LIST,
            # Generic types not supported yet like this: https://github.com/python/mypy/issues/4717
            Sequence[str],  # type: ignore
        )

    @staticmethod
    def _from_metadata_type(
        name: str, metadata_type: str
    ) -> Optional[SearchAttributeKey]:
        if metadata_type == "Text":
            return SearchAttributeKey.for_text(name)
        elif metadata_type == "Keyword":
            return SearchAttributeKey.for_keyword(name)
        elif metadata_type == "Int":
            return SearchAttributeKey.for_int(name)
        elif metadata_type == "Double":
            return SearchAttributeKey.for_float(name)
        elif metadata_type == "Bool":
            return SearchAttributeKey.for_bool(name)
        elif metadata_type == "Datetime":
            return SearchAttributeKey.for_datetime(name)
        elif metadata_type == "KeywordList":
            return SearchAttributeKey.for_keyword_list(name)
        return None

    @staticmethod
    def _guess_from_untyped_values(
        name: str, vals: SearchAttributeValues
    ) -> Optional[SearchAttributeKey]:
        if not vals:
            return None
        elif len(vals) > 1:
            if isinstance(vals[0], str):
                return SearchAttributeKey.for_keyword_list(name)
        elif isinstance(vals[0], str):
            return SearchAttributeKey.for_keyword(name)
        elif isinstance(vals[0], int):
            return SearchAttributeKey.for_int(name)
        elif isinstance(vals[0], float):
            return SearchAttributeKey.for_float(name)
        elif isinstance(vals[0], bool):
            return SearchAttributeKey.for_bool(name)
        elif isinstance(vals[0], datetime):
            return SearchAttributeKey.for_datetime(name)
        return None


@dataclass(frozen=True)
class _SearchAttributeKey(SearchAttributeKey[SearchAttributeValueType]):
    _name: str
    _indexed_value_type: SearchAttributeIndexedValueType
    # No supported way in Python to derive this, so we're setting manually
    _value_type: Type[SearchAttributeValueType]

    @property
    def name(self) -> str:
        return self._name

    @property
    def indexed_value_type(self) -> SearchAttributeIndexedValueType:
        return self._indexed_value_type

    @property
    def value_type(self) -> Type[SearchAttributeValueType]:
        return self._value_type


class SearchAttributePair(NamedTuple, Generic[SearchAttributeValueType]):
    """A named tuple representing a key/value search attribute pair."""

    key: SearchAttributeKey[SearchAttributeValueType]
    value: SearchAttributeValueType


class SearchAttributeUpdate(ABC, Generic[SearchAttributeValueType]):
    """Representation of a search attribute update."""

    @property
    @abstractmethod
    def key(self) -> SearchAttributeKey[SearchAttributeValueType]:
        """Key that is being set."""
        ...

    @property
    @abstractmethod
    def value(self) -> Optional[SearchAttributeValueType]:
        """Value that is being set or ``None`` if being unset."""
        ...


@dataclass(frozen=True)
class _SearchAttributeUpdate(SearchAttributeUpdate[SearchAttributeValueType]):
    _key: SearchAttributeKey[SearchAttributeValueType]
    _value: Optional[SearchAttributeValueType]

    @property
    def key(self) -> SearchAttributeKey[SearchAttributeValueType]:
        return self._key

    @property
    def value(self) -> Optional[SearchAttributeValueType]:
        return self._value


@dataclass(frozen=True)
class TypedSearchAttributes(Collection[SearchAttributePair]):
    """Collection of typed search attributes.

    This is represented as an immutable collection of
    :py:class:`SearchAttributePair`. This can be created passing a sequence of
    pairs to the constructor.
    """

    search_attributes: Sequence[SearchAttributePair]
    """Underlying sequence of search attribute pairs. Do not mutate this, only
    create new ``TypedSearchAttribute`` instances.

    These are sorted by key name during construction. Duplicates cannot exist.
    """

    empty: ClassVar[TypedSearchAttributes]
    """Class variable representing an empty set of attributes."""

    def __post_init__(self):
        """Post-init initialization."""
        # Sort
        object.__setattr__(
            self,
            "search_attributes",
            sorted(self.search_attributes, key=lambda pair: pair.key.name),
        )
        # Ensure no duplicates
        for i, pair in enumerate(self.search_attributes):
            if i > 0 and self.search_attributes[i - 1].key.name == pair.key.name:
                raise ValueError(
                    f"Duplicate search attribute entries found for key {pair.key.name}"
                )

    def __len__(self) -> int:
        """Get the number of search attributes."""
        return len(self.search_attributes)

    def __getitem__(
        self, key: SearchAttributeKey[SearchAttributeValueType]
    ) -> SearchAttributeValueType:
        """Get a single search attribute value by key or fail with
        ``KeyError``.
        """
        ret = next((v for k, v in self if k == key), None)
        if ret is None:
            raise KeyError()
        return ret

    def __iter__(self) -> Iterator[SearchAttributePair]:
        """Get an iterator over search attribute key/value pairs."""
        return iter(self.search_attributes)

    def __contains__(self, key: object) -> bool:
        """Check whether this search attribute contains the given key.

        This uses key equality so the key must be the same name and type.
        """
        return any(v for k, v in self if k == key)

    @overload
    def get(
        self, key: SearchAttributeKey[SearchAttributeValueType]
    ) -> Optional[SearchAttributeValueType]:
        ...

    @overload
    def get(
        self,
        key: SearchAttributeKey[SearchAttributeValueType],
        default: temporalio.types.AnyType,
    ) -> Union[SearchAttributeValueType, temporalio.types.AnyType]:
        ...

    def get(
        self,
        key: SearchAttributeKey[SearchAttributeValueType],
        default: Optional[Any] = None,
    ) -> Any:
        """Get an attribute value for a key (or default). This is similar to
        dict.get.
        """
        try:
            return self.__getitem__(key)
        except KeyError:
            return default

    def updated(self, *search_attributes: SearchAttributePair) -> TypedSearchAttributes:
        """Copy this collection, replacing attributes with matching key names or
        adding if key name not present.
        """
        attrs = list(self.search_attributes)
        # Go over each update, replacing matching keys by index or adding
        for attr in search_attributes:
            existing_index = next(
                (i for i, attr in enumerate(attrs) if attr.key.name == attr.key.name),
                None,
            )
            if existing_index is None:
                attrs.append(attr)
            else:
                attrs[existing_index] = attr
        return TypedSearchAttributes(attrs)


TypedSearchAttributes.empty = TypedSearchAttributes(search_attributes=[])


def _warn_on_deprecated_search_attributes(
    attributes: Optional[Union[SearchAttributes, Any]],
    stack_level: int = 2,
) -> None:
    if attributes and isinstance(attributes, Mapping):
        warnings.warn(
            "Dictionary-based search attributes are deprecated",
            DeprecationWarning,
            stacklevel=1 + stack_level,
        )


MetricAttributes: TypeAlias = Mapping[str, Union[str, int, float, bool]]


class MetricMeter(ABC):
    """Metric meter for recording metrics."""

    noop: ClassVar[MetricMeter]
    """Metric meter implementation that does nothing."""

    @abstractmethod
    def create_counter(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricCounter:
        """Create a counter metric for adding values.

        Args:
            name: Name for the metric.
            description: Optional description for the metric.
            unit: Optional unit for the metric.

        Returns:
            Counter metric.
        """
        ...

    @abstractmethod
    def create_histogram(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricHistogram:
        """Create a histogram metric for recording values.

        Args:
            name: Name for the metric.
            description: Optional description for the metric.
            unit: Optional unit for the metric.

        Returns:
            Histogram metric.
        """
        ...

    @abstractmethod
    def create_gauge(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricGauge:
        """Create a gauge metric for setting values.

        Args:
            name: Name for the metric.
            description: Optional description for the metric.
            unit: Optional unit for the metric.

        Returns:
            Gauge metric.
        """
        ...

    @abstractmethod
    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricMeter:
        """Create a new metric meter with the given attributes appended to the
        current set.

        Args:
            additional_attributes: Additional attributes to append to the
                current set.

        Returns:
            New metric meter.

        Raises:
            TypeError: Attribute values are not the expected type.
        """
        ...


class MetricCounter(ABC):
    """Counter metric created by a metric meter."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name for the metric."""
        ...

    @property
    @abstractmethod
    def description(self) -> Optional[str]:
        """Description for the metric if any."""
        ...

    @property
    @abstractmethod
    def unit(self) -> Optional[str]:
        """Unit for the metric if any."""
        ...

    @abstractmethod
    def add(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        """Add a value to the counter.

        Args:
            value: A non-negative integer to add.
            additional_attributes: Additional attributes to append to the
                current set.

        Raises:
            ValueError: Value is negative.
            TypeError: Attribute values are not the expected type.
        """
        ...

    @abstractmethod
    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricCounter:
        """Create a new counter with the given attributes appended to the
        current set.

        Args:
            additional_attributes: Additional attributes to append to the
                current set.

        Returns:
            New counter.

        Raises:
            TypeError: Attribute values are not the expected type.
        """
        ...


class MetricHistogram(ABC):
    """Histogram metric created by a metric meter."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name for the metric."""
        ...

    @property
    @abstractmethod
    def description(self) -> Optional[str]:
        """Description for the metric if any."""
        ...

    @property
    @abstractmethod
    def unit(self) -> Optional[str]:
        """Unit for the metric if any."""
        ...

    @abstractmethod
    def record(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        """Record a value on the histogram.

        Args:
            value: A non-negative integer to record.
            additional_attributes: Additional attributes to append to the
                current set.

        Raises:
            ValueError: Value is negative.
            TypeError: Attribute values are not the expected type.
        """
        ...

    @abstractmethod
    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricHistogram:
        """Create a new histogram with the given attributes appended to the
        current set.

        Args:
            additional_attributes: Additional attributes to append to the
                current set.

        Returns:
            New histogram.

        Raises:
            TypeError: Attribute values are not the expected type.
        """
        ...


class MetricGauge(ABC):
    """Gauge metric created by a metric meter."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name for the metric."""
        ...

    @property
    @abstractmethod
    def description(self) -> Optional[str]:
        """Description for the metric if any."""
        ...

    @property
    @abstractmethod
    def unit(self) -> Optional[str]:
        """Unit for the metric if any."""
        ...

    @abstractmethod
    def set(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        """Set a value on the gauge.

        Args:
            value: A non-negative integer to set.
            additional_attributes: Additional attributes to append to the
                current set.

        Raises:
            ValueError: Value is negative.
            TypeError: Attribute values are not the expected type.
        """
        ...

    @abstractmethod
    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricGauge:
        """Create a new gauge with the given attributes appended to the
        current set.

        Args:
            additional_attributes: Additional attributes to append to the
                current set.

        Returns:
            New gauge.

        Raises:
            TypeError: Attribute values are not the expected type.
        """
        ...


class _NoopMetricMeter(MetricMeter):
    def create_counter(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricCounter:
        return _NoopMetricCounter(name, description, unit)

    def create_histogram(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricHistogram:
        return _NoopMetricHistogram(name, description, unit)

    def create_gauge(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> MetricGauge:
        return _NoopMetricGauge(name, description, unit)

    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricMeter:
        return self


class _NoopMetric:
    def __init__(
        self, name: str, description: Optional[str], unit: Optional[str]
    ) -> None:
        self._name = name
        self._description = description
        self._unit = unit

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def unit(self) -> Optional[str]:
        return self._unit


class _NoopMetricCounter(_NoopMetric, MetricCounter):
    def add(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        pass

    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricCounter:
        return self


class _NoopMetricHistogram(_NoopMetric, MetricHistogram):
    def record(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        pass

    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricHistogram:
        return self


class _NoopMetricGauge(_NoopMetric, MetricGauge):
    def set(
        self, value: int, additional_attributes: Optional[MetricAttributes] = None
    ) -> None:
        pass

    def with_additional_attributes(
        self, additional_attributes: MetricAttributes
    ) -> MetricGauge:
        return self


MetricMeter.noop = _NoopMetricMeter()

# Should be set as the "arg" argument for _arg_or_args checks where the argument
# is unset. This is different than None which is a legitimate argument.
_arg_unset = object()


def _arg_or_args(arg: Any, args: Sequence[Any]) -> Sequence[Any]:
    if arg is not _arg_unset:
        if args:
            raise ValueError("Cannot have arg and args")
        args = [arg]
    return args


def _apply_headers(
    source: Optional[Mapping[str, temporalio.api.common.v1.Payload]],
    dest: google.protobuf.internal.containers.MessageMap[
        Text, temporalio.api.common.v1.Payload
    ],
) -> None:
    if source is None:
        return
    # Due to how protobuf maps of messages work, we cannot just set these or
    # "update" these, instead they expect a shallow copy
    # TODO(cretz): We could make this cheaper where we use it by precreating the
    # command, but that forces proto commands to be embedded into interceptor
    # inputs.
    for k, v in source.items():
        # This does not copy bytes, just messages
        dest[k].CopyFrom(v)


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
        and not isinstance(func, _non_user_defined_callables)
        and not isinstance(func, types.MethodType)
    ):
        # Class type or Callable instance
        tmp_func = func if isinstance(func, type) else type(func)
        call_func = getattr(tmp_func, "__call__", None)
        if call_func is not None and not isinstance(
            tmp_func, _non_user_defined_callables
        ):
            func = call_func

    # We use inspect.signature for the parameter names and kinds, but we cannot
    # use it for annotations because those that are using deferred hinting (i.e.
    # from __future__ import annotations) only work with the eval_str parameter
    # which is only supported in >= 3.10. But typing.get_type_hints is supported
    # in >= 3.7.
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    ret_hint = hints.get("return")
    ret = ret_hint if ret_hint is not inspect.Signature.empty else None
    args: List[Type] = []
    for index, value in enumerate(sig.parameters.values()):
        # Ignore self on methods
        if (
            index == 0
            and value.name == "self"
            and (
                value.annotation is inspect.Parameter.empty
                or str(value.annotation) == "typing.Self"
            )
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
        if arg_hint is inspect.Parameter.empty:
            return (None, ret)
        # Ignoring type here because union/optional isn't really a type
        # necessarily
        args.append(arg_hint)  # type: ignore
    return args, ret
