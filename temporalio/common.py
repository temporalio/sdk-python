"""Common code used in the Temporal SDK."""

from __future__ import annotations

import inspect
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from typing import (
    Any,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Text,
    Tuple,
    Type,
    Union,
    get_type_hints,
)

import google.protobuf.internal.containers
from typing_extensions import ClassVar, TypeAlias

import temporalio.api.common.v1
import temporalio.api.enums.v1


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
