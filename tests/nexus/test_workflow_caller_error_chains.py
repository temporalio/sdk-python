from __future__ import annotations

import uuid
from dataclasses import dataclass

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
    NexusOperationError,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@dataclass
class ExpectedNexusOperationError:
    message: str
    service: str


@dataclass
class ExpectedHandlerError:
    message: str  # Uses containment check (server may add prefix)
    type: str  # HandlerErrorType name (e.g., "INTERNAL", "NOT_FOUND")
    retryable: bool


@dataclass
class ExpectedApplicationError:
    message: str
    non_retryable: bool
    type: str | None = None


ExpectedExceptionInfo = (
    ExpectedNexusOperationError | ExpectedHandlerError | ExpectedApplicationError
)


@dataclass
class ErrorTestCase:
    name: str
    operation_name: str
    expected_exception_chain: list[ExpectedExceptionInfo]


class CustomError(Exception):
    pass


@dataclass
class ErrorTestInput:
    task_queue: str
    operation_name: str
    expected_exception_chain: list[ExpectedExceptionInfo]


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
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="application-error-message",
            type="INTERNAL",
            retryable=False,
        ),
        ExpectedApplicationError(
            message="application-error-message",
            type="application-error-type",
            non_retryable=True,
        ),
    ],
)


RaiseApplicationErrorNonRetryableFromCustomError = ErrorTestCase(
    name="RaiseApplicationErrorNonRetryableFromCustomError",
    operation_name=ErrorTestService.raise_application_error_non_retryable_from_custom_error.__name__,
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="application-error-message",
            type="INTERNAL",
            retryable=False,
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
    ],
)


RaiseNexusHandlerErrorNotFound = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFound",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found.__name__,
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedApplicationError(
            message="handler-error-message",
            non_retryable=True,
        ),
        ExpectedApplicationError(
            message="runtime-error-message",
            type="RuntimeError",
            non_retryable=False,
        ),
    ],
)


RaiseNexusHandlerErrorNotFoundFromCustomError = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFoundFromCustomError",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found_from_custom_error.__name__,
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedApplicationError(
            message="handler-error-message",
            non_retryable=True,
        ),
        ExpectedApplicationError(
            message="custom-error-message",
            type="CustomError",
            non_retryable=False,
        ),
    ],
)


RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable = ErrorTestCase(
    name="RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable",
    operation_name=ErrorTestService.raise_nexus_handler_error_not_found_from_handler_error_unavailable.__name__,
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
        ),
        ExpectedHandlerError(
            message="handler-error-message",
            type="NOT_FOUND",
            retryable=False,
        ),
        ExpectedApplicationError(
            message="handler-error-message",
            non_retryable=True,
        ),
        ExpectedHandlerError(
            message="handler-error-message-2",
            type="UNAVAILABLE",
            retryable=True,
        ),
    ],
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
    expected_exception_chain=[
        ExpectedNexusOperationError(
            message="nexus operation completed unsuccessfully",
            service="ErrorTestService",
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
    ],
)


# Caller workflow


def _validate_exception_chain(
    err: BaseException,
    expected_chain: list[ExpectedExceptionInfo],
) -> None:
    """Walk the exception chain and validate each exception against expected."""
    actual_chain: list[BaseException] = []
    current: BaseException | None = err
    while current is not None:
        actual_chain.append(current)
        current = current.__cause__

    assert len(actual_chain) == len(
        expected_chain
    ), f"Expected {len(expected_chain)} exceptions in chain, got {len(actual_chain)}"

    for actual, expected in zip(actual_chain, expected_chain):
        if isinstance(expected, ExpectedNexusOperationError):
            assert isinstance(
                actual, NexusOperationError
            ), f"Expected NexusOperationError, got {type(actual).__name__}"
            assert (
                actual.message == expected.message
            ), f"Expected message '{expected.message}', got '{actual.message}'"
            assert (
                actual.service == expected.service
            ), f"Expected service '{expected.service}', got '{actual.service}'"

        elif isinstance(expected, ExpectedHandlerError):
            assert isinstance(
                actual, nexusrpc.HandlerError
            ), f"Expected HandlerError, got {type(actual).__name__}"
            # HandlerError message may have server prefix, check containment
            actual_message = str(actual)
            assert (
                expected.message in actual_message
            ), f"Expected message containing '{expected.message}', got '{actual_message}'"
            assert (
                actual.type.name == expected.type
            ), f"Expected handler_error_type '{expected.type}', got '{actual.type.name}'"
            assert (
                actual.retryable == expected.retryable
            ), f"Expected retryable={expected.retryable}, got {actual.retryable}"

        elif isinstance(expected, ExpectedApplicationError):
            assert isinstance(
                actual, ApplicationError
            ), f"Expected ApplicationError, got {type(actual).__name__}"
            assert (
                actual.message == expected.message
            ), f"Expected message '{expected.message}', got '{actual.message}'"
            assert (
                actual.non_retryable == expected.non_retryable
            ), f"Expected non_retryable={expected.non_retryable}, got {actual.non_retryable}"
            if expected.type is not None:
                assert (
                    actual.type == expected.type
                ), f"Expected type '{expected.type}', got '{actual.type}'"


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
            _validate_exception_chain(err, input.expected_exception_chain)
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
        await create_nexus_endpoint(task_queue, client)
        await client.execute_workflow(
            ErrorTestCallerWorkflow.run,
            ErrorTestInput(
                task_queue=task_queue,
                operation_name=test_case.operation_name,
                expected_exception_chain=test_case.expected_exception_chain,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
