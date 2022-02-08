from enum import IntEnum
from typing import Any, Iterable, Optional

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.converter


class TemporalError(Exception):
    pass


class FailureError(TemporalError):
    def __init__(
        self,
        message: str,
        *details: Any,
        failure: Optional[temporalio.api.failure.v1.Failure] = None
    ) -> None:
        super().__init__(message)
        self._details = details
        self._failure = failure

    @property
    def details(self) -> Iterable[Any]:
        return self._details

    @property
    def failure(self) -> Optional[temporalio.api.failure.v1.Failure]:
        return self._failure


class ApplicationError(FailureError):
    def __init__(
        self,
        message: str,
        *details: Any,
        type: Optional[str] = None,
        non_retryable: bool = False
    ) -> None:
        super().__init__(message, *details)
        self._type = type
        self._non_retryable = non_retryable

    @property
    def type(self) -> Optional[str]:
        return self._type

    @property
    def non_retryable(self) -> bool:
        return self._non_retryable


class CancelledError(FailureError):
    def __init__(self, message: str, *details: Any) -> None:
        super().__init__(message, *details)


class TerminatedError(FailureError):
    def __init__(self, message: str, *details: Any) -> None:
        super().__init__(message, *details)


class TimeoutType(IntEnum):
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
    def __init__(
        self,
        message: str,
        *,
        type: Optional[TimeoutType],
        last_heartbeat_details: Iterable[Any]
    ) -> None:
        super().__init__(message)
        self._type = type
        self._last_heartbeat_details = last_heartbeat_details

    @property
    def type(self) -> Optional[TimeoutType]:
        return self._type

    @property
    def last_heartbeat_details(self) -> Iterable[Any]:
        return self._last_heartbeat_details


class ServerError(FailureError):
    def __init__(self, message: str, *, non_retryable: bool = False) -> None:
        super().__init__(message)
        self._non_retryable = non_retryable

    @property
    def non_retryable(self) -> bool:
        return self._non_retryable


class RetryState(IntEnum):
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
    def __init__(
        self,
        message: str,
        *,
        scheduled_event_id: int,
        started_event_id: int,
        identity: str,
        activity_type: str,
        activity_id: str,
        retry_state: Optional[RetryState]
    ) -> None:
        super().__init__(message)
        self._scheduled_event_id = scheduled_event_id
        self._started_event_id = started_event_id
        self._identity = identity
        self._activity_type = activity_type
        self._activity_id = activity_id
        self._retry_state = retry_state

    @property
    def scheduled_event_id(self) -> int:
        return self._scheduled_event_id

    @property
    def started_event_id(self) -> int:
        return self._started_event_id

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def activity_type(self) -> str:
        return self._activity_type

    @property
    def activity_id(self) -> str:
        return self._activity_id

    @property
    def retry_state(self) -> Optional[RetryState]:
        return self._retry_state


class ChildWorkflowError(FailureError):
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
        retry_state: Optional[RetryState]
    ) -> None:
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
        return self._namespace

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def workflow_type(self) -> str:
        return self._workflow_type

    @property
    def initiated_event_id(self) -> int:
        return self._initiated_event_id

    @property
    def started_event_id(self) -> int:
        return self._started_event_id

    @property
    def retry_state(self) -> Optional[RetryState]:
        return self._retry_state


async def failure_to_error(
    failure: temporalio.api.failure.v1.Failure,
    converter: temporalio.converter.DataConverter,
) -> FailureError:
    err: FailureError
    if failure.HasField("application_failure_info"):
        app_info = failure.application_failure_info
        err = ApplicationError(
            failure.message,
            *(await temporalio.converter.decode_payloads(app_info.details, converter)),
            type=app_info.type or None,
            non_retryable=app_info.non_retryable
        )
    elif failure.HasField("timeout_failure_info"):
        timeout_info = failure.timeout_failure_info
        err = TimeoutError(
            failure.message,
            type=TimeoutType(int(timeout_info.timeout_type))
            if timeout_info.timeout_type
            else None,
            last_heartbeat_details=await temporalio.converter.decode_payloads(
                timeout_info.last_heartbeat_details, converter
            ),
        )
    elif failure.HasField("canceled_failure_info"):
        cancel_info = failure.canceled_failure_info
        err = CancelledError(
            failure.message,
            await temporalio.converter.decode_payloads(cancel_info.details, converter),
        )
    elif failure.HasField("terminated_failure_info"):
        err = TerminatedError(failure.message)
    elif failure.HasField("server_failure_info"):
        server_info = failure.server_failure_info
        err = ServerError(failure.message, non_retryable=server_info.non_retryable)
    elif failure.HasField("activity_failure_info"):
        act_info = failure.activity_failure_info
        err = ActivityError(
            failure.message,
            scheduled_event_id=act_info.scheduled_event_id,
            started_event_id=act_info.started_event_id,
            identity=act_info.identity,
            activity_type=act_info.activity_type.name,
            activity_id=act_info.activity_id,
            retry_state=RetryState(int(act_info.retry_state))
            if act_info.retry_state
            else None,
        )
    elif failure.HasField("child_workflow_execution_failure_info"):
        child_info = failure.child_workflow_execution_failure_info
        err = ChildWorkflowError(
            failure.message,
            namespace=child_info.namespace,
            workflow_id=child_info.workflow_execution.workflow_id,
            run_id=child_info.workflow_execution.run_id,
            workflow_type=child_info.workflow_type.name,
            initiated_event_id=child_info.initiated_event_id,
            started_event_id=child_info.started_event_id,
            retry_state=RetryState(int(child_info.retry_state))
            if child_info.retry_state
            else None,
        )
    else:
        err = FailureError(failure.message)
    err._failure = failure
    if failure.HasField("cause"):
        err.__cause__ = await failure_to_error(failure.cause, converter)
    return err
