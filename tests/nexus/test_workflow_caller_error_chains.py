from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from tests.helpers.nexus import make_nexus_endpoint_name

error_conversion_test_cases: dict[str, type[ErrorConversionTestCase]] = {}


@dataclass
class ErrorConversionTestCase:
    action_in_nexus_operation: Callable[..., Any]
    expected_exception_chain_in_workflow: list[tuple[type[Exception], dict[str, Any]]]

    def __init_subclass__(cls, **kwargs):  # type:ignore[reportMissingParameterType]
        super().__init_subclass__(**kwargs)
        assert cls.__name__ not in error_conversion_test_cases
        error_conversion_test_cases[cls.__name__] = cls


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


class RaiseApplicationErrorNonRetryable(ErrorConversionTestCase):
    @staticmethod
    def action_in_nexus_operation():
        raise ApplicationError(
            "application-error-message",
            type="application-error-type",
            non_retryable=True,
        )

    expected_exception_chain_in_workflow = [
        (
            NexusOperationError,
            {
                "service": "ErrorTestService",
                "message": "nexus operation completed unsuccessfully",
            },
        ),
        (
            nexusrpc.HandlerError,
            {
                # In this test case the user code raised ApplicationError directly, and
                # a wrapping HandlerError was synthesized with the same error message as
                # that of the ApplicationError. The server prepends 'handler error
                # (INTERNAL):'
                "message": "handler error (INTERNAL): application-error-message",
                "type": nexusrpc.HandlerErrorType.INTERNAL,
                "retryable": False,
            },
        ),
        (
            ApplicationError,
            {
                "message": "application-error-message",
                "type": "application-error-type",
                "non_retryable": True,
            },
        ),
    ]


class RaiseApplicationErrorNonRetryableFromCustomError(ErrorConversionTestCase):
    @staticmethod
    def action_in_nexus_operation():
        try:
            raise CustomError("custom-error-message")
        except CustomError as err:
            raise ApplicationError(
                "application-error-message",
                type="application-error-type",
                non_retryable=True,
            ) from err

    expected_exception_chain_in_workflow = (
        RaiseApplicationErrorNonRetryable.expected_exception_chain_in_workflow
        + [
            (
                ApplicationError,
                {
                    "message": "custom-error-message",
                    "type": "CustomError",
                    "non_retryable": False,
                },
            ),
        ]
    )


class RaiseNexusHandlerErrorNotFound(ErrorConversionTestCase):
    @staticmethod
    def action_in_nexus_operation():
        try:
            raise RuntimeError("runtime-error-message")
        except RuntimeError as err:
            raise nexusrpc.HandlerError(
                "handler-error-message",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            ) from err

    expected_exception_chain_in_workflow = [
        (
            NexusOperationError,
            {
                "service": "ErrorTestService",
                "message": "nexus operation completed unsuccessfully",
            },
        ),
        (
            nexusrpc.HandlerError,
            {
                # In this test case the user code raised HandlerError directly, so there
                # was no need to synthesize a wrapping HandlerError The server prepends
                # 'handler error (INTERNAL):'
                "message": "handler error (NOT_FOUND): handler-error-message",
                "type": nexusrpc.HandlerErrorType.NOT_FOUND,
                # The following HandlerError types should be considered non-retryable:
                # BAD_REQUEST, UNAUTHENTICATED, UNAUTHORIZED, NOT_FOUND, and
                # RESOURCE_EXHAUSTED. In this test case, the handler does not set the
                # retryable flag in the HandlerError sent to the server. This value is
                # computed by the retryable property on HandlerError.
                "retryable": False,
            },
        ),
        (
            ApplicationError,
            {
                "message": "handler-error-message",
                "non_retryable": True,
            },
        ),
        (
            ApplicationError,
            {
                "message": "runtime-error-message",
                "type": "RuntimeError",
                "non_retryable": False,
            },
        ),
    ]


class RaiseNexusHandlerErrorNotFoundFromCustomError(ErrorConversionTestCase):
    @staticmethod
    def action_in_nexus_operation():
        try:
            raise CustomError("custom-error-message")
        except CustomError as err:
            raise nexusrpc.HandlerError(
                "handler-error-message",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            ) from err

    expected_exception_chain_in_workflow = (
        RaiseNexusHandlerErrorNotFound.expected_exception_chain_in_workflow[:-1]
        + [
            (
                ApplicationError,
                {
                    # TODO(nexus-preview): empirically, this is "handler-error-message",
                    # but it should be "runtime-error-message"
                    # "message": "runtime-error-message",
                    "type": "CustomError",
                    "non_retryable": False,
                },
            )
        ]
    )


class RaiseNexusHandlerErrorNotFoundFromHandlerErrorUnavailable(
    ErrorConversionTestCase
):
    @staticmethod
    def action_in_nexus_operation():
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

    expected_exception_chain_in_workflow = (
        RaiseNexusHandlerErrorNotFound.expected_exception_chain_in_workflow[:-1]
        + [
            (
                nexusrpc.HandlerError,
                {
                    "message": "handler-error-message-2",
                    "type": nexusrpc.HandlerErrorType.UNAVAILABLE,
                    "retryable": True,
                },
            )
        ]
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
#
class RaiseNexusOperationErrorFromApplicationErrorNonRetryableFromCustomError(
    ErrorConversionTestCase
):
    @staticmethod
    def action_in_nexus_operation():
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

    expected_exception_chain_in_workflow = [
        (
            NexusOperationError,
            {
                "message": "nexus operation completed unsuccessfully",
                "service": "ErrorTestService",
            },
        ),
        (
            ApplicationError,
            {
                "message": "application-error-message",
                "type": "application-error-type",
                "non_retryable": True,
            },
        ),
        (
            ApplicationError,
            {
                "message": "custom-error-message",
                "type": "CustomError",
                "non_retryable": False,
            },
        ),
    ]


class CustomError(Exception):
    pass


@dataclass
class ErrorTestInput:
    task_queue: str
    name: str


@nexusrpc.handler.service_handler
class ErrorTestService:
    @sync_operation
    async def op(self, _ctx: StartOperationContext, input: ErrorTestInput) -> None:
        error_conversion_test_cases[input.name].action_in_nexus_operation()


# Caller


@workflow.defn(sandboxed=False)
class ErrorTestCallerWorkflow:
    @workflow.init
    def __init__(self, input: ErrorTestInput):
        self.nexus_client = workflow.create_nexus_client(
            service=ErrorTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )

    @workflow.run
    async def invoke_nexus_op_and_assert_error(self, input: ErrorTestInput) -> None:
        try:
            await self.nexus_client.execute_operation(
                ErrorTestService.op,  # type: ignore[arg-type] # mypy can't infer OutputT=None in Union type
                input,
                output_type=None,
            )
        except BaseException as err:
            errs = [err]
            while err.__cause__:
                errs.append(err.__cause__)
                err = err.__cause__

            test_case = error_conversion_test_cases[input.name]
            assert len(errs) == len(test_case.expected_exception_chain_in_workflow)
            for err, (expected_cls, expected_fields) in zip(
                errs, test_case.expected_exception_chain_in_workflow
            ):
                assert isinstance(err, expected_cls)
                for k, v in expected_fields.items():
                    if k == "message" and isinstance(err, nexusrpc.HandlerError):
                        assert str(err) == v
                    else:
                        assert getattr(err, k) == v

        else:
            assert False, "Unreachable"


@pytest.mark.parametrize("test_case", list(error_conversion_test_cases.values()))
async def test_errors_raised_by_nexus_operation(
    client: Client, env: WorkflowEnvironment, test_case: type[ErrorConversionTestCase]
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
            ErrorTestCallerWorkflow.invoke_nexus_op_and_assert_error,
            ErrorTestInput(
                task_queue=task_queue,
                name=test_case.__name__,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
