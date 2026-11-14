"""Common Temporal exceptions."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from enum import IntEnum
from typing import Any

import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.stream.v1


class TemporalError(Exception):
    """Base for all Temporal exceptions."""

    @property
    def cause(self) -> BaseException | None:
        """Cause of the exception.

        This is the same as ``Exception.__cause__``.
        """
        return self.__cause__


class FailureError(TemporalError):
    """Base for runtime failures during workflow/activity execution."""

    def __init__(
        self,
        message: str,
        *,
        failure: temporalio.api.failure.v1.Failure | None = None,
        exc_args: tuple | None = None,
    ) -> None:
        """Initialize a failure error."""
        if exc_args is None:
            exc_args = (message,)
        super().__init__(*exc_args)
        self._message = message
        self._failure = failure

    @property
    def message(self) -> str:
        """Message."""
        return self._message

    @property
    def failure(self) -> temporalio.api.failure.v1.Failure | None:
        """Underlying protobuf failure object."""
        return self._failure


class WorkflowAlreadyStartedError(FailureError):
    """Thrown by a client or workflow when a workflow execution has already started.

    Attributes:
        workflow_id: ID of the already-started workflow.
        workflow_type: Workflow type name of the already-started workflow.
        run_id: Run ID of the already-started workflow if this was raised by the
            client.
        first_run_id: First execution run ID of the already-started workflow if
            this was raised by the client and provided by the server.
    """

    def __init__(
        self,
        workflow_id: str,
        workflow_type: str,
        *,
        run_id: str | None = None,
        first_run_id: str | None = None,
    ) -> None:
        """Initialize a workflow already started error."""
        super().__init__("Workflow execution already started")
        self.workflow_id = workflow_id
        self.workflow_type = workflow_type
        self.run_id = run_id
        self.first_run_id = first_run_id


class InvalidVersioningOverrideError(FailureError):
    """Thrown by a workflow when a child cannot start because its versioning
    override is invalid.
    """

    def __init__(self) -> None:
        """Initialize an invalid versioning override error."""
        super().__init__("Invalid versioning override")


class ActivityAlreadyStartedError(FailureError):
    """Thrown by a client when an activity execution has already started.

    Attributes:
        activity_id: ID of the already-started activity.
        activity_type: Activity type name of the already-started activity.
        run_id: Run ID of the already-started activity if this was raised by the
            client.
    """

    def __init__(
        self, activity_id: str, activity_type: str, *, run_id: str | None = None
    ) -> None:
        """Initialize an activity already started error."""
        super().__init__("Activity execution already started")
        self.activity_id = activity_id
        self.activity_type = activity_type
        self.run_id = run_id


class NexusOperationAlreadyStartedError(FailureError):
    """Thrown by a client when a Nexus operation execution has already started.

    .. warning::
       This API is experimental and unstable.

    Attributes:
        operation_id: ID of the already-started operation.
        run_id: Run ID of the already-started operation if available.
    """

    def __init__(self, operation_id: str, *, run_id: str | None = None) -> None:
        """Initialize a Nexus operation already started error."""
        super().__init__("Nexus operation execution already started")
        self.operation_id = operation_id
        self.run_id = run_id


class ApplicationErrorCategory(IntEnum):
    """Severity category for your application error. Maps to corresponding client-side logging/metrics behaviors"""

    UNSPECIFIED = int(
        temporalio.api.enums.v1.ApplicationErrorCategory.APPLICATION_ERROR_CATEGORY_UNSPECIFIED
    )

    BENIGN = int(
        temporalio.api.enums.v1.ApplicationErrorCategory.APPLICATION_ERROR_CATEGORY_BENIGN
    )
    """BENIGN category errors emit DEBUG level logs and do not record metrics"""


class ApplicationError(FailureError):
    """Error raised during workflow/activity execution."""

    def __init__(
        self,
        message: str,
        *details: Any,
        type: str | None = None,
        non_retryable: bool = False,
        next_retry_delay: timedelta | None = None,
        category: ApplicationErrorCategory = ApplicationErrorCategory.UNSPECIFIED,
    ) -> None:
        """Initialize an application error."""
        super().__init__(
            message,
            # If there is a type, prepend it to the message on the string repr
            exc_args=(message if not type else f"{type}: {message}",),
        )
        self._details = details
        self._type = type
        self._non_retryable = non_retryable
        self._next_retry_delay = next_retry_delay
        self._category = category

    @property
    def details(self) -> Sequence[Any]:
        """User-defined details on the error."""
        return self._details

    @property
    def type(self) -> str | None:
        """General error type."""
        return self._type

    @property
    def non_retryable(self) -> bool:
        """Whether the error was set as non-retryable when created.

        Note: This is not whether the error is non-retryable via other means
        such as retry policy. This is just whether the error was marked
        non-retryable upon creation by the user.
        """
        return self._non_retryable

    @property
    def next_retry_delay(self) -> timedelta | None:
        """Delay before the next activity retry attempt.

        User activity code may set this when raising ApplicationError to specify
        a delay before the next activity retry.
        """
        return self._next_retry_delay

    @property
    def category(self) -> ApplicationErrorCategory:
        """Severity category of the application error"""
        return self._category


_STREAM_ERROR_REASON_UNSPECIFIED = (
    temporalio.api.enums.v1.StreamErrorReason.STREAM_ERROR_REASON_UNSPECIFIED
)

# The one reason that a retry can fix.
_STREAM_RETRYABLE_REASONS: frozenset[int] = frozenset(
    {temporalio.api.enums.v1.StreamErrorReason.STREAM_ERROR_REASON_STORAGE_UNAVAILABLE}
)


class StreamError(ApplicationError):
    """A failed Stream call. ``reason`` tells the cases apart.

    One class covers every failed call in client, Activity and Workflow code, so match
    on ``reason`` and not on the type. The reason is a ``StreamErrorReason``, such as
    ``STREAM_ERROR_REASON_NOT_FOUND`` (nobody created the Stream),
    ``STREAM_ERROR_REASON_CLOSED`` (the Stream is closed), or
    ``STREAM_ERROR_REASON_ALREADY_EXISTS`` (a strict create of a Stream that exists).
    A Store outage, ``STREAM_ERROR_REASON_STORAGE_UNAVAILABLE``, is the one retryable
    reason.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: temporalio.api.enums.v1.StreamErrorReason.ValueType | None = None,
    ) -> None:
        """Initialize a Stream error. ``reason`` defaults to the unspecified reason."""
        if reason is None:
            reason = _STREAM_ERROR_REASON_UNSPECIFIED
        # The details carry the reason, so the error keeps it when it crosses an
        # Activity or a Nexus operation.
        super().__init__(
            message,
            temporalio.api.stream.v1.StreamErrorDetails(reason=reason),
            type="StreamError",
            non_retryable=reason not in _STREAM_RETRYABLE_REASONS,
        )
        self._reason = reason

    @property
    def reason(self) -> temporalio.api.enums.v1.StreamErrorReason.ValueType:
        """Why the Stream call failed."""
        return self._reason


def _stream_error_from_reason(reason: int, message: str) -> StreamError:
    if reason not in temporalio.api.enums.v1.StreamErrorReason.values():
        # A reason that this SDK doesn't know gives the unspecified reason.
        reason = _STREAM_ERROR_REASON_UNSPECIFIED
    return StreamError(
        message, reason=temporalio.api.enums.v1.StreamErrorReason.ValueType(reason)
    )


def _stream_error_from_call(  # pyright: ignore[reportUnusedFunction]
    message: str, details: bytes
) -> StreamError:
    # A Store on the bridge fails with the encoded details, as a gRPC status has them.
    proto = temporalio.api.stream.v1.StreamErrorDetails.FromString(details)
    return _stream_error_from_reason(proto.reason, message)


def _stream_error_from_failure(  # pyright: ignore[reportUnusedFunction]
    err: BaseException,
) -> StreamError | None:
    # A Local Activity or a Nexus operation hands back a plain application error, so the
    # error is rebuilt from its type and details.
    if isinstance(err, StreamError):
        return err
    if not isinstance(err, ApplicationError) or err.type != "StreamError":
        return None
    reason: int = _STREAM_ERROR_REASON_UNSPECIFIED
    for detail in err.details:
        if isinstance(detail, temporalio.api.stream.v1.StreamErrorDetails):
            reason = detail.reason
            break
    return _stream_error_from_reason(reason, err.message)


class StreamConfigError(RuntimeError):
    """A Stream call can't find its Store, because of how the Stores are set up.

    It is not a :py:class:`StreamError`. A Store didn't fail. The setup of the Stores
    is wrong, and a retry won't help. These are its cases.

    * A Stream call is made on a client with no Stream config. This release has no
      Streams on the server, so every Stream call needs a Store.
    * A Workflow's Stream call is made on a Worker whose client has no Stream config.
      This fails the Workflow Task.
    * A :py:class:`temporalio.client.StreamConfig` has no Store, has two Stores with
      one name, or has more than one Store and no selector.
    * A ref names a Store that the config of the client doesn't have.
    * The selector returns a Store that isn't one of the config's Stores.
    * A :py:class:`temporalio.client.StreamStore` is given in ``plugins``.
    * A Workflow's Stream call has a ref that names a Store that the config of the
      Worker's client doesn't have. This fails the Workflow Task.
    * This build of temporalio has no Stream runtime, or no Redis Stream Store.

    .. warning::
        This API is experimental and subject to change.
    """


class CancelledError(FailureError):
    """Error raised on workflow/activity cancellation."""

    def __init__(self, message: str = "Cancelled", *details: Any) -> None:
        """Initialize a cancelled error."""
        super().__init__(message)
        self._details = details

    @property
    def details(self) -> Sequence[Any]:
        """User-defined details on the error."""
        return self._details


class TerminatedError(FailureError):
    """Error raised on workflow cancellation."""

    def __init__(self, message: str, *details: Any) -> None:
        """Initialize a terminated error."""
        super().__init__(message)
        self._details = details

    @property
    def details(self) -> Sequence[Any]:
        """User-defined details on the error."""
        return self._details


class TimeoutType(IntEnum):
    """Type of timeout for :py:class:`TimeoutError`."""

    START_TO_CLOSE = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_START_TO_CLOSE
    )
    SCHEDULE_TO_START = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_START
    )
    SCHEDULE_TO_CLOSE = int(
        temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
    )
    HEARTBEAT = int(temporalio.api.enums.v1.TimeoutType.TIMEOUT_TYPE_HEARTBEAT)


class TimeoutError(FailureError):
    """Error raised on workflow/activity timeout."""

    def __init__(
        self,
        message: str,
        *,
        type: TimeoutType | None,
        last_heartbeat_details: Sequence[Any],
    ) -> None:
        """Initialize a timeout error."""
        super().__init__(message)
        self._type = type
        self._last_heartbeat_details = last_heartbeat_details

    @property
    def type(self) -> TimeoutType | None:
        """Type of timeout error."""
        return self._type

    @property
    def last_heartbeat_details(self) -> Sequence[Any]:
        """Last heartbeat details if this is for an activity heartbeat."""
        return self._last_heartbeat_details


class ServerError(FailureError):
    """Error originating in the Temporal server."""

    def __init__(self, message: str, *, non_retryable: bool = False) -> None:
        """Initialize a server error."""
        super().__init__(message)
        self._non_retryable = non_retryable

    @property
    def non_retryable(self) -> bool:
        """Whether this error is non-retryable."""
        return self._non_retryable


class RetryState(IntEnum):
    """Current retry state of the workflow/activity during error."""

    IN_PROGRESS = int(temporalio.api.enums.v1.RetryState.RETRY_STATE_IN_PROGRESS)
    NON_RETRYABLE_FAILURE = int(
        temporalio.api.enums.v1.RetryState.RETRY_STATE_NON_RETRYABLE_FAILURE
    )
    TIMEOUT = int(temporalio.api.enums.v1.RetryState.RETRY_STATE_TIMEOUT)
    MAXIMUM_ATTEMPTS_REACHED = int(
        temporalio.api.enums.v1.RetryState.RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED
    )
    RETRY_POLICY_NOT_SET = int(
        temporalio.api.enums.v1.RetryState.RETRY_STATE_RETRY_POLICY_NOT_SET
    )
    INTERNAL_SERVER_ERROR = int(
        temporalio.api.enums.v1.RetryState.RETRY_STATE_INTERNAL_SERVER_ERROR
    )
    CANCEL_REQUESTED = int(
        temporalio.api.enums.v1.RetryState.RETRY_STATE_CANCEL_REQUESTED
    )


class ActivityError(FailureError):
    """Error raised on activity failure."""

    def __init__(
        self,
        message: str,
        *,
        scheduled_event_id: int,
        started_event_id: int,
        identity: str,
        activity_type: str,
        activity_id: str,
        retry_state: RetryState | None,
    ) -> None:
        """Initialize an activity error."""
        super().__init__(message)
        self._scheduled_event_id = scheduled_event_id
        self._started_event_id = started_event_id
        self._identity = identity
        self._activity_type = activity_type
        self._activity_id = activity_id
        self._retry_state = retry_state

    @property
    def scheduled_event_id(self) -> int:
        """Scheduled event ID for this error."""
        return self._scheduled_event_id

    @property
    def started_event_id(self) -> int:
        """Started event ID for this error."""
        return self._started_event_id

    @property
    def identity(self) -> str:
        """Identity for this error."""
        return self._identity

    @property
    def activity_type(self) -> str:
        """Activity type for this error."""
        return self._activity_type

    @property
    def activity_id(self) -> str:
        """Activity ID for this error."""
        return self._activity_id

    @property
    def retry_state(self) -> RetryState | None:
        """Retry state for this error."""
        return self._retry_state


class ChildWorkflowError(FailureError):
    """Error raised on child workflow failure."""

    def __init__(
        self,
        message: str,
        *,
        namespace: str,
        workflow_id: str,
        run_id: str,
        workflow_type: str,
        initiated_event_id: int,
        started_event_id: int,
        retry_state: RetryState | None,
    ) -> None:
        """Initialize a child workflow error."""
        super().__init__(message)
        self._namespace = namespace
        self._workflow_id = workflow_id
        self._run_id = run_id
        self._workflow_type = workflow_type
        self._initiated_event_id = initiated_event_id
        self._started_event_id = started_event_id
        self._retry_state = retry_state

    @property
    def namespace(self) -> str:
        """Namespace for this error."""
        return self._namespace

    @property
    def workflow_id(self) -> str:
        """Workflow ID for this error."""
        return self._workflow_id

    @property
    def run_id(self) -> str:
        """Run ID for this error."""
        return self._run_id

    @property
    def workflow_type(self) -> str:
        """Workflow type for this error."""
        return self._workflow_type

    @property
    def initiated_event_id(self) -> int:
        """Initiated event ID for this error."""
        return self._initiated_event_id

    @property
    def started_event_id(self) -> int:
        """Started event ID for this error."""
        return self._started_event_id

    @property
    def retry_state(self) -> RetryState | None:
        """Retry state for this error."""
        return self._retry_state


class NexusOperationError(FailureError):
    """Error raised on Nexus operation failure inside a Workflow."""

    def __init__(
        self,
        message: str,
        *,
        scheduled_event_id: int,
        endpoint: str,
        service: str,
        operation: str,
        operation_token: str,
    ):
        """Initialize a Nexus operation error.

        Args:
            message: The error message.
            scheduled_event_id: The NexusOperationScheduled event ID for the failed operation.
            endpoint: The endpoint name for the failed operation.
            service: The service name for the failed operation.
            operation: The name of the failed operation.
            operation_token: The operation token returned by the failed operation.
        """
        super().__init__(message)
        self._scheduled_event_id = scheduled_event_id
        self._endpoint = endpoint
        self._service = service
        self._operation = operation
        self._operation_token = operation_token

    @property
    def scheduled_event_id(self) -> int:
        """The NexusOperationScheduled event ID for the failed operation."""
        return self._scheduled_event_id

    @property
    def endpoint(self) -> str:
        """The endpoint name for the failed operation."""
        return self._endpoint

    @property
    def service(self) -> str:
        """The service name for the failed operation."""
        return self._service

    @property
    def operation(self) -> str:
        """The name of the failed operation."""
        return self._operation

    @property
    def operation_token(self) -> str:
        """The operation token returned by the failed operation."""
        return self._operation_token


def is_cancelled_exception(exception: BaseException) -> bool:
    """Check whether the given exception is considered a cancellation exception
    according to Temporal.

    This is often used in a conditional of a catch clause to check whether a
    cancel occurred inside of a workflow. This can occur from
    :py:class:`asyncio.CancelledError` or :py:class:`CancelledError` or either
    :py:class:`ActivityError` or :py:class:`ChildWorkflowError` if either of
    those latter two have a :py:class:`CancelledError` cause.

    Args:
        exception: Exception to check.

    Returns:
        True if a cancelled exception, false if not.
    """
    return (
        isinstance(exception, asyncio.CancelledError)
        or isinstance(exception, CancelledError)
        or (
            (
                isinstance(exception, ActivityError)
                or isinstance(exception, ChildWorkflowError)
                or isinstance(exception, NexusOperationError)
            )
            and isinstance(exception.cause, CancelledError)
        )
    )
