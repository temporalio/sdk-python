"""Common code used in the Temporal SDK."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any, List, Mapping, Optional, Sequence, Text, Union

import google.protobuf.internal.containers
from typing_extensions import TypeAlias

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


# We choose to make this a list instead of an sequence so we can catch if people
# are not sending lists each time but maybe accidentally sending a string (which
# is a sequence)
SearchAttributeValues: TypeAlias = Union[
    List[str], List[int], List[float], List[bool], List[datetime]
]

SearchAttributes: TypeAlias = Mapping[str, SearchAttributeValues]


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
