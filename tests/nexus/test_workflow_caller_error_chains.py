from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    StartOperationContext,
    sync_operation,
)

from temporalio import workflow
from temporalio.client import (
    Client,
)
from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    NexusOperationError,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class ExpectedError:
    message: str
    optional: bool = field(kw_only=True, default=False)


@dataclass
class ExpectedNexusOperationError(ExpectedError):
    service: str


@dataclass
class ExpectedHandlerError(ExpectedError):
    type: str  # HandlerErrorType name (e.g., "INTERNAL", "NOT_FOUND")
    retryable: bool


@dataclass
class ExpectedApplicationError(ExpectedError):
    non_retryable: bool
    type: str | None = None


@dataclass
class ExpectedCancelledError(ExpectedError):
    pass


class ExpectedErrorChain:
    def __init__(self, *errs: ExpectedError) -> None:
        self._errs = errs

    def required_len(self) -> int:
        return sum(not e.optional for e in self._errs)

    def __len__(self) -> int:
        return len(self._errs)

    def __iter__(self) -> Iterator[ExpectedError]:
        return iter(self._errs)

    def __getitem__(self, i: int) -> ExpectedError:
        return self._errs[i]


ExpectedExceptionInfo = (
    ExpectedNexusOperationError
    | ExpectedHandlerError
    | ExpectedApplicationError
    | ExpectedCancelledError
)


@dataclass
class ErrorTestCase:
    name: str
    operation_name: str
    expected_exception_chain: ExpectedErrorChain


class CustomError(Exception):
    pass


@dataclass
class ErrorTestInput:
    task_queue: str
    operation_name: str


# Handler service with one operation per test case


@nexusrpc.handler.service_handler
class ErrorTestService:
    @sync_operation
    async def raise_application_error_non_retryable(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        raise ApplicationError(
            "application-error-message",
            type="application-error-type",
            non_retryable=True,
        )

    @sync_operation
    async def raise_application_error_non_retryable_from_custom_error(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            raise CustomError("custom-error-message")
        except CustomError as err:
            raise ApplicationError(
                "application-error-message",
                type="application-error-type",
                non_retryable=True,
            ) from err

    @sync_operation
    async def raise_nexus_handler_error_not_found(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            raise RuntimeError("runtime-error-message")
        except RuntimeError as err:
            raise nexusrpc.HandlerError(
                "handler-error-message",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            ) from err

    @sync_operation
    async def raise_nexus_handler_error_not_found_from_custom_error(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            raise CustomError("custom-error-message")
        except CustomError as err:
            raise nexusrpc.HandlerError(
                "handler-error-message",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            ) from err

    @sync_operation
    async def raise_nexus_handler_error_not_found_from_handler_error_unavailable(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            raise nexusrpc.HandlerError(
                "handler-error-message-2",
                type=nexusrpc.HandlerErrorType.UNAVAILABLE,
            )
        except nexusrpc.HandlerError as err:
            raise nexusrpc.HandlerError(
                "handler-error-message",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            ) from err

    @sync_operation
    async def raise_nexus_operation_error_from_application_error_non_retryable_from_custom_error(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            try:
                raise CustomError("custom-error-message")
            except CustomError as err:
                raise ApplicationError(
                    "application-error-message",
                    type="application-error-type",
                    non_retryable=True,
                ) from err
        except ApplicationError as err:
            raise nexusrpc.OperationError(
                "operation-error-message",
                state=nexusrpc.OperationErrorState.FAILED,
            ) from err

    @sync_operation
    async def raise_nexus_operation_canceled_error_from_application_error_non_retryable_from_custom_error(
        self, _ctx: StartOperationContext, _input: ErrorTestInput
    ) -> None:
        try:
            try:
                raise CustomError("custom-error-message")
            except CustomError as err:
                raise ApplicationError(
                    "application-error-message",
                    type="application-error-type",
                    non_retryable=True,
                ) from err
        except ApplicationError as err:
            raise nexusrpc.OperationError(
                "operation-error-message",
                state=nexusrpc.OperationErrorState.CANCELED,
            ) from err


# Test cases
#
# If a nexus handler raises a non-retryable ApplicationError, the calling workflow
# should see a non-retryable exception.
#
# The Java handler sends NexusTaskFailed containing
#
# temporalio.api.nexus.v1.HandlerError(INTERNAL, RETRY_BEHAVIOR_NON_RETRYABLE, failure={
#     message: "application-error-message",
#     details: [
#         ApplicationErrorInfo(non_retryable, "application-error-type", <no message>)
#     ]
#   }
# )
#
# The Java workflow caller rehydrates this as below. Essentially, the error chain is
# NexusOperationError: corresponds to the NexusTaskFailed request perhaps
#     nexusrpc.HandlerError: represents the top-level HandlerError proto (non_retryable=True from the HandlerError proto retry_behavior)
#         ApplicationFailure: represents the first (and only) item in the failure details chain.
#
# io.temporal.failure.NexusOperationFailure(message="Nexus Operation with operation='testErrorservice='NexusService' endpoint='my-nexus-endpoint-name' failed: 'nexus operation completed unsuccessfully'. scheduledEventId=5, operationToken=", scheduledEventId=scheduledEventId, operationToken="operationToken")
#     io.nexusrpc.handler.HandlerException(message="handler error: message='application-error-message', type='application-error-type', nonRetryable=true", type="INTERNAL", nonRetryable=true)
#         io.temporal.failure.ApplicationFailure(message="application-error-message", type="application-error-type", nonRetryable=true)
#
# The Python handler sends NexusTaskFailed containing
#
# temporalio.api.nexus.v1.HandlerError(INTERNAL, RETRY_BEHAVIOR_NON_RETRYABLE, failure={
#     message: "application-error-message",
#     details: [
#         ApplicationErrorInfo("application-error-type", non_retryable, "application-error-message")
#     ]
#   }
# )
RaiseApplicationErrorNonRetryable = ErrorTestCase(
    name="RaiseApplicationErrorNonRetryable",
    operation_name=ErrorTestService.raise_application_error_non_retryable.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="Handler failed with non-retryable application error",
            type="INTERNAL",
            retryable=False,
        ),
        ExpectedHandlerError(
            message="Handler failed with non-retryable application error",
            type="INTERNAL",
            retryable=False,
            optional=True,
        ),
        ExpectedApplicationError(
            message="application-error-message",
            type="application-error-type",
            non_retryable=True,
        ),
    ),
)


RaiseApplicationErrorNonRetryableFromCustomError = ErrorTestCase(
    name="RaiseApplicationErrorNonRetryableFromCustomError",
    operation_name=ErrorTestService.raise_application_error_non_retryable_from_custom_error.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="Handler failed with non-retryable application error",
            type="INTERNAL",
            retryable=False,
        ),
        ExpectedHandlerError(
            message="Handler failed with non-retryable application error",
            type="INTERNAL",
            retryable=False,
            optional=True,
        ),
        ExpectedApplicationError(
            message="application-error-message",
            type="application-error-type",
            non_retryable=True,
        ),
        ExpectedApplicationError(
            message="custom-error-message",
            type="CustomError",
            non_retryable=False,
        ),
    ),
)


RaiseNexusHandlerErrorNotFound = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFound",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
            optional=True,
        ),
        ExpectedApplicationError(
            message="runtime-error-message",
            type="RuntimeError",
            non_retryable=False,
        ),
    ),
)


RaiseNexusHandlerErrorNotFoundFromCustomError = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFoundFromCustomError",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found_from_custom_error.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
            optional=True,
        ),
        ExpectedApplicationError(
            message="custom-error-message",
            type="CustomError",
            non_retryable=False,
        ),
    ),
)


RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found_from_handler_error_unavailable.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
            optional=True,
        ),
        ExpectedHandlerError(
            message="handler-error-message-2",
            type="UNAVAILABLE",
            retryable=True,
        ),
    ),
)


# If a nexus handler raises an OperationError, the calling workflow
# should see a non-retryable exception.
#
# Given Java operation code
#
#   throw OperationException.failure(
#       ApplicationFailure.newNonRetryableFailureWithCause(
#           "application-error-message",
#           "application-error-type",
#           new MyCustomException("Custom error 2")));
#
# the Java handler sends NexusTaskCompleted containing
#
# temporalio.api.nexus.v1.UnsuccessfulOperationError(FAILED, failure={
#     message: "application-error-message",
#     details: [
#         ApplicationErrorInfo(non_retryable, "application-error-type", <no message>),
#         ApplicationErrorInfo(retryable, "MyCustomException", "custom-error-message"),
#     ]
#   }
# )
#
# The Java workflow caller rehydrates this as below. Essentially, the error chain is
# NexusOperationError: corresponds to the top-level UnsuccessfulOperationError
#     ApplicationError: corresponds to the 1st ApplicationError in the details chain
#         ApplicationError: corresponds to the 2nd ApplicationError in the details chain
#
# io.temporal.failure.NexusOperationFailure(message="Nexus Operation with operation='testErrorservice='NexusService' endpoint='my-nexus-endpoint-name' failed: 'nexus operation completed unsuccessfully'. scheduledEventId=5, operationToken=", scheduledEventId=scheduledEventId, operationToken="operationToken")
#     io.temporal.failure.ApplicationFailure(message="application-error-message", type="application-error-type", nonRetryable=true)
#         io.temporal.failure.ApplicationFailure(message="Custom error 2", type="io.temporal.samples.nexus.handler.NexusServiceImpl$MyCustomException", nonRetryable=false)
#
# The Python handler sends NexusTaskCompleted containing
# temporalio.api.nexus.v1.UnsuccessfulOperationError(FAILED, failure={
#     message: "operation-error-message",
#     details: [
#         ApplicationErrorInfo("OperationError", retryable,  <no message>),
#         ApplicationErrorInfo("application-error-type", non_retryable, "application-error-message"),
#         ApplicationErrorInfo("CustomError", retryable, "custom-error-message"),
#     ]
#   }
# )
RaiseNexusOperationErrorFromApplicationErrorNonRetryableFromCustomError = ErrorTestCase(
    name="RaiseNexusOperationErrorFromApplicationErrorNonRetryableFromCustomError",
    operation_name=ErrorTestService.raise_nexus_operation_error_from_application_error_non_retryable_from_custom_error.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedApplicationError(
            message="operation-error-message", type="OperationError", non_retryable=True
        ),
        ExpectedApplicationError(
            message="application-error-message",
            type="application-error-type",
            non_retryable=True,
        ),
        ExpectedApplicationError(
            message="custom-error-message",
            type="CustomError",
            non_retryable=False,
        ),
    ),
)

RaiseNexusOperationCanceledErrorFromApplicationErrorNonRetryableFromCustomError = ErrorTestCase(
    name="RaiseNexusOperationCanceledErrorFromApplicationErrorNonRetryableFromCustomError",
    operation_name=ErrorTestService.raise_nexus_operation_canceled_error_from_application_error_non_retryable_from_custom_error.__name__,
    expected_exception_chain=ExpectedErrorChain(
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedCancelledError(
            message="operation-error-message",
        ),
        ExpectedApplicationError(
            message="application-error-message",
            type="application-error-type",
            non_retryable=True,
        ),
        ExpectedApplicationError(
            message="custom-error-message",
            type="CustomError",
            non_retryable=False,
        ),
    ),
)


_EXPECTED_CHAINS: dict[str, ExpectedErrorChain] = {
    tc.operation_name: tc.expected_exception_chain
    for tc in [
        RaiseApplicationErrorNonRetryable,
        RaiseApplicationErrorNonRetryableFromCustomError,
        RaiseNexusHandlerErrorNotFound,
        RaiseNexusHandlerErrorNotFoundFromCustomError,
        RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable,
        RaiseNexusOperationErrorFromApplicationErrorNonRetryableFromCustomError,
        RaiseNexusOperationCanceledErrorFromApplicationErrorNonRetryableFromCustomError,
    ]
}


# Caller workflow


def _matches_expected(actual: BaseException, expected: ExpectedError) -> bool:
    """Check if an actual exception matches the expected error specification."""
    if isinstance(expected, ExpectedNexusOperationError):
        if not isinstance(actual, NexusOperationError):
            return False
        if actual.message != expected.message:
            return False
        if actual.service != expected.service:
            return False
        return True

    elif isinstance(expected, ExpectedHandlerError):
        if not isinstance(actual, nexusrpc.HandlerError):
            return False
        if expected.message not in str(actual):
            return False
        if actual.type.name != expected.type:
            return False
        if actual.retryable != expected.retryable:
            return False
        return True

    elif isinstance(expected, ExpectedApplicationError):
        if not isinstance(actual, ApplicationError):
            return False
        if actual.message != expected.message:
            return False
        if actual.non_retryable != expected.non_retryable:
            return False
        if expected.type is not None and actual.type != expected.type:
            return False
        return True

    elif isinstance(expected, ExpectedCancelledError):
        if not isinstance(actual, CancelledError):
            return False
        if actual.message != expected.message:
            return False
        return True

    return False


def _format_mismatch(actual: BaseException, expected: ExpectedError) -> str:
    """Format a detailed mismatch message for debugging."""
    lines = ["Mismatch between actual and expected error:"]
    lines.append(f"  Actual: {type(actual).__name__}: {actual}")
    lines.append(f"  Expected: {expected}")
    return "\n".join(lines)


def _validate_exception_chain(
    err: BaseException,
    expected_chain: ExpectedErrorChain,
) -> None:
    """Walk the exception chain and validate each exception against expected.

    Optional expected errors can be skipped if they don't match the current actual error.
    """
    actual_chain: list[BaseException] = []
    current: BaseException | None = err
    while current is not None:
        actual_chain.append(current)
        current = current.__cause__

    actual_idx = 0
    expected_idx = 0

    while actual_idx < len(actual_chain) and expected_idx < len(expected_chain):
        actual = actual_chain[actual_idx]
        expected = expected_chain[expected_idx]

        if _matches_expected(actual, expected):
            # Match found, advance both
            actual_idx += 1
            expected_idx += 1
        elif expected.optional:
            # Optional expected error didn't match, skip it
            print(f"Skipping optional expected error: {expected}")
            expected_idx += 1
        else:
            # Required expected error didn't match
            assert False, _format_mismatch(actual, expected)

    # Check remaining expected errors are all optional
    while expected_idx < len(expected_chain):
        expected = expected_chain[expected_idx]
        assert (
            expected.optional
        ), f"Required expected error not found in chain: {expected}"
        expected_idx += 1

    # Check no remaining actual errors
    assert actual_idx == len(
        actual_chain
    ), f"Unexpected errors in chain: {actual_chain[actual_idx:]}"


@workflow.defn(sandboxed=False)
class ErrorTestCallerWorkflow:
    @workflow.init
    def __init__(self, input: ErrorTestInput):
        self.nexus_client = workflow.create_nexus_client(
            service=ErrorTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )

    @workflow.run
    async def run(self, input: ErrorTestInput) -> None:
        try:
            await self.nexus_client.execute_operation(
                input.operation_name,
                input,
                output_type=None,
            )
        except BaseException as err:
            _validate_exception_chain(err, _EXPECTED_CHAINS[input.operation_name])
            return

        raise AssertionError("Expected exception was not raised")


@pytest.mark.parametrize(
    "test_case",
    [
        RaiseApplicationErrorNonRetryable,
        RaiseApplicationErrorNonRetryableFromCustomError,
        RaiseNexusHandlerErrorNotFound,
        RaiseNexusHandlerErrorNotFoundFromCustomError,
        RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable,
        RaiseNexusOperationErrorFromApplicationErrorNonRetryableFromCustomError,
        RaiseNexusOperationCanceledErrorFromApplicationErrorNonRetryableFromCustomError,
    ],
    ids=lambda tc: tc.name,
)
async def test_errors_raised_by_nexus_operation(
    client: Client, env: WorkflowEnvironment, test_case: ErrorTestCase
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ErrorTestService()],
        workflows=[ErrorTestCallerWorkflow],
        task_queue=task_queue,
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        await client.execute_workflow(
            ErrorTestCallerWorkflow.run,
            ErrorTestInput(
                task_queue=task_queue,
                operation_name=test_case.operation_name,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
