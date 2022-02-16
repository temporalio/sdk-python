"""Common code used in the Temporal SDK."""

from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum
from typing import Iterable, Optional

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

    non_retryable_error_types: Optional[Iterable[str]] = None
    """List of error types that are not retryable."""

    def apply_to_proto(self, proto: temporalio.api.common.v1.RetryPolicy) -> None:
        """Apply the fields in this policy to the given proto object."""
        proto.initial_interval.FromTimedelta(self.initial_interval)
        proto.backoff_coefficient = self.backoff_coefficient
        proto.maximum_interval.FromTimedelta(
            self.maximum_interval or self.initial_interval * 100
        )
        proto.maximum_attempts = self.maximum_attempts
        if self.non_retryable_error_types:
            proto.non_retryable_error_types.extend(self.non_retryable_error_types)


class WorkflowIDReusePolicy(IntEnum):
    """How already-in-use workflow IDs are handled on start.

    See :py:class:`temporalio.api.enums.v1.WorkflowIdReusePolicy`.
    """

    ALLOW_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE`."""

    ALLOW_DUPLICATE_FAILED_ONLY = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY`."""

    REJECT_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE`."""


class QueryRejectCondition(IntEnum):
    """Whether a query should be rejected in certain conditions.

    See :py:class:`temporalio.api.enums.v1.QueryRejectCondition`.
    """

    NONE = int(temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NONE)
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NONE`."""

    NOT_OPEN = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_OPEN
    )
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_OPEN`."""

    NOT_COMPLETED_CLEANLY = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_COMPLETED_CLEANLY
    )
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_COMPLETED_CLEANLY`."""
