"""
See https://github.com/nexus-rpc/api/blob/main/SPEC.md

This file contains test coverage for Nexus StartOperation and CancelOperation
operations issued by a caller directly via HTTP.

The response to StartOperation may indicate a protocol-level failure (400
BAD_REQUEST, 520 UPSTREAM_TIMEOUT, etc). In this case the body is a valid
Failure object.


(https://github.com/nexus-rpc/api/blob/main/SPEC.md#predefined-handler-errors)

"""

import asyncio
import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Never, Optional, Tuple, Type

import httpx
import nexusrpc
import nexusrpc.handler
import pytest
from google.protobuf import json_format
from hyperlinked import print

import temporalio.api.failure.v1
import temporalio.nexus
from temporalio import workflow
from temporalio.client import Client
from temporalio.converter import FailureConverter, PayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.nexus import logger
from temporalio.nexus.handler import StartWorkflowOperationResult, start_workflow
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint


@dataclass
class Input:
    value: str


@dataclass
class Output:
    value: str


# TODO(dan): tests are currently not failing when this interface doesn't match
@nexusrpc.interface.service
class MyService:
    echo: nexusrpc.interface.Operation[Input, Output]
    hang: nexusrpc.interface.Operation[Input, Output]
    log: nexusrpc.interface.Operation[Input, Output]
    error: nexusrpc.interface.Operation[Input, Output]
    async_operation: nexusrpc.interface.Operation[Input, Output]


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from workflow: {input.value}")


@nexusrpc.handler.service(interface=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation
    async def echo(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        assert options.headers["test-header-key"] == "test-header-value"
        return Output(value=f"from handler: {input.value}")

    @nexusrpc.handler.sync_operation
    async def hang(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Never:
        await asyncio.Future()

    @nexusrpc.handler.sync_operation
    async def non_retryable_application_error(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        raise ApplicationError(
            "non-retryable application error",
            "details arg",
            # TODO(dan): what values of `type` should be tested?
            type="TestFailureType",
            non_retryable=True,
        )

    @nexusrpc.handler.sync_operation
    async def retryable_application_error(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        raise ApplicationError(
            "retryable application error",
            "details arg",
            type="TestFailureType",
            non_retryable=False,
        )

    @nexusrpc.handler.sync_operation
    async def handler_error_internal(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        raise nexusrpc.handler.HandlerError(
            message="deliberate internal handler error",
            type=nexusrpc.handler.HandlerErrorType.INTERNAL,
            retryable=False,
            cause=RuntimeError("cause message"),
        )

    @nexusrpc.handler.sync_operation
    async def operation_error_failed(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        raise nexusrpc.handler.OperationError(
            message="deliberate operation error",
            state=nexusrpc.handler.OperationErrorState.FAILED,
        )

    @nexusrpc.handler.sync_operation
    async def log(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        logger.info("Logging from handler", extra={"input_value": input.value})
        return Output(value=f"logged: {input.value}")

    @temporalio.nexus.handler.workflow_run_operation
    async def async_operation(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> StartWorkflowOperationResult[Output]:
        return await start_workflow(
            MyWorkflow.run,
            input,
            id=str(uuid.uuid4()),
            options=options,
        )


@dataclass
class Failure:
    message: str = ""
    metadata: Optional[dict[str, str]] = None
    details: Optional[dict[str, Any]] = None

    exception: Optional[BaseException] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.metadata and (error_type := self.metadata.get("type")):
            self.exception = self._instantiate_exception(error_type, self.details)

    def _instantiate_exception(
        self, error_type: str, details: Optional[dict[str, Any]]
    ) -> BaseException:
        proto = {
            "temporal.api.failure.v1.Failure": temporalio.api.failure.v1.Failure,
        }[error_type]()
        json_format.ParseDict(self.details, proto, ignore_unknown_fields=True)
        return FailureConverter.default.from_failure(proto, PayloadConverter.default)


class _TestCase:
    operation: str
    input: str = ""
    headers: dict[str, str] = {}
    expected_status_code: int

    @staticmethod
    def check_response_body(response: dict[str, Any]) -> None:
        pass

    @staticmethod
    def check_response_headers(headers: dict[str, str]) -> None:
        pass


class _FailureTestCase(_TestCase):
    retryable: Optional[bool] = None

    @staticmethod
    def check_failure(failure: Failure) -> None:
        pass


class SyncHandlerHappyPath(_TestCase):
    operation = "echo"
    input = "hello"
    expected_status_code = 200
    headers = {
        "Content-Type": "application/json",
        "Test-Header-Key": "test-header-value",
        "Nexus-Link": '<http://test/>; type="test"',
    }

    @staticmethod
    def check_response_body(body: dict[str, Any]) -> None:
        assert body["value"] == "from handler: hello"

    @staticmethod
    def check_response_headers(headers: dict[str, str]) -> None:
        # TODO(dan): Support manually adding links in operation handler
        # See e.g. TS nexus.handlerLinks().push(...options.links)
        # assert headers.get("nexus-link") == "<http://test/>; type=\"test\"", \
        #     "Nexus-Link header not echoed correctly."
        pass


class AsyncHandlerHappyPath(_TestCase):
    operation = "async_operation"
    input = "hello"
    expected_status_code = 201
    headers = {
        "Content-Type": "application/json",
    }


# TODO(dan): Before fixing the upstream-timeout test by implementing the handler for the
# timeout cancellation sent by core, I was seeing 2025-05-11T22:41:51.853243Z  WARN
# temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value
# '5.617792ms'

# TODO(dan): test Nexus-Callback- headers


class UpstreamTimeout(_FailureTestCase):
    operation = "hang"
    # TODO(dan): test Operation-Timeout header also?
    headers = {"Request-Timeout": "10ms"}
    expected_status_code = 520

    @staticmethod
    def check_response_body(response: dict[str, Any]) -> None:
        assert response["message"] == "upstream timeout"


class BadRequest(_FailureTestCase):
    operation = "echo"
    input = 7  # type: ignore
    expected_status_code = 400
    # TODO(dan): This should be marked non-retryable
    retryable = False

    @staticmethod
    def check_response_body(response: dict[str, Any]) -> None:
        failure = Failure(**response)
        assert "Failed converting field" in failure.message
        assert failure.metadata == {"type": "temporal.api.failure.v1.Failure"}

    @staticmethod
    def check_failure(failure: Failure) -> None:
        # TODO(dan): is it correct that this is ApplicationError?
        assert isinstance(failure.exception, ApplicationError)


class NonRetryableApplicationError(_FailureTestCase):
    operation = "non_retryable_application_error"
    expected_status_code = 500
    retryable = False

    @staticmethod
    def check_failure(failure: Failure) -> None:
        assert failure.metadata == {"type": "temporal.api.failure.v1.Failure"}
        assert failure.message == "non-retryable application error"
        # TODO(dan): Why are there two levels of ApplicationError? The inner one is non-retryable.
        err = failure.exception
        assert isinstance(err, ApplicationError)
        err = err.cause
        assert isinstance(err, ApplicationError)
        assert err.non_retryable
        assert err.type == "TestFailureType"
        assert err.details == ("details arg",)


class RetryableApplicationError(_FailureTestCase):
    operation = "retryable_application_error"
    expected_status_code = 500
    retryable = True


class HandlerErrorInternal(_FailureTestCase):
    operation = "handler_error_internal"
    expected_status_code = 500
    # TODO(dan): check this assertion
    retryable = False

    @staticmethod
    def check_failure(failure: Failure) -> None:
        assert failure.metadata == {"type": "temporal.api.failure.v1.Failure"}
        assert failure.message == "deliberate internal handler error"
        assert failure.exception.cause is not None
        assert failure.exception.cause.message == "cause message"


class OperationError(_FailureTestCase):
    operation = "operation_error_failed"
    # TODO(dan): 424
    expected_status_code = 500
    retryable = False
    headers = {"nexus-operation-state": "failed"}

    @staticmethod
    def check_failure(failure: Failure) -> None:
        assert failure.metadata == {"type": "temporal.api.failure.v1.Failure"}
        assert failure.message == "deliberate operation error"


@pytest.mark.parametrize(
    "test_case",
    [
        SyncHandlerHappyPath,
        AsyncHandlerHappyPath,
    ],
)
async def test_start_operation_happy_path(
    test_case: Type[_TestCase], http_test_env: Tuple[Client, int]
):
    await _test_start_operation(test_case, http_test_env)


@pytest.mark.parametrize(
    "test_case",
    [
        UpstreamTimeout,
        BadRequest,
        HandlerErrorInternal,
    ],
)
async def test_start_operation_protocol_level_failures(
    test_case: Type[_TestCase], http_test_env: Tuple[Client, int]
):
    await _test_start_operation(test_case, http_test_env)


@pytest.mark.parametrize(
    "test_case",
    [
        NonRetryableApplicationError,
        RetryableApplicationError,
        OperationError,
    ],
)
async def test_start_operation_operation_failures(
    test_case: Type[_TestCase], http_test_env: Tuple[Client, int]
):
    await _test_start_operation(test_case, http_test_env)


async def _test_start_operation(
    test_case: Type[_TestCase], http_test_env: Tuple[Client, int]
):
    client, http_port = http_test_env
    task_queue = str(uuid.uuid4())
    service = MyService.__name__
    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{http_port}/nexus/endpoints/{endpoint}/services/{service}/{test_case.operation}",
                json={"value": test_case.input},
                headers=test_case.headers,
            )
            assert response.status_code == test_case.expected_status_code
            test_case.check_response_body(response.json())
            test_case.check_response_headers(dict(response.headers))
            if issubclass(test_case, _FailureTestCase):
                failure = Failure(**response.json())
                test_case.check_failure(failure)
                if test_case.retryable is not None:
                    assert (
                        json.loads(response.headers["nexus-request-retryable"])
                        == test_case.retryable
                    )
                err = failure.exception
                print(
                    f"got err {err.__class__} with retryable headers {response.headers.get('nexus-request-retryable')}"
                )
                if isinstance(err, ApplicationError):
                    print(
                        f"🍎 ApplicationError attr {err.retryable},  -> {getattr(err.cause, 'retryable', None)}"
                    )
                    # assert test_case.retryable == (not err.non_retryable)
                    # if test_case.retryable != err.retryable:
                    #     print(
                    #         f"\n\n🔴 TODO(dan): failed retryable assertion (expected {test_case.retryable}, got {err.retryable})",
                    #     )
                # else:
                #     # TODO(dan): handle other error types
                #     raise NotImplementedError(f"Unknown error type: {type(err)}")

    print(
        "\n\n----------------------------------------------------------------------\n\n"
    )


async def test_logger_uses_operation_context(
    http_test_env: Tuple[Client, int], caplog: Any
):
    client, http_port = http_test_env
    task_queue = str(uuid.uuid4())
    service_name = MyService.__name__
    operation_name = "log"
    resp = await create_nexus_endpoint(task_queue, client)
    endpoint = resp.endpoint.id

    caplog.set_level(logging.INFO)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{http_port}/nexus/endpoints/{endpoint}/services/{service_name}/{operation_name}",
                json={"value": "test_log"},
                headers={
                    "Content-Type": "application/json",
                    "Test-Log-Header": "test-log-header-value",
                },
            )
            assert response.is_success
            response.raise_for_status()
            output_json = response.json()
            assert output_json == {"value": "logged: test_log"}

    record = next(
        (
            record
            for record in caplog.records
            if record.name == "temporalio.nexus"
            and record.getMessage() == "Logging from handler"
        ),
        None,
    )
    assert record is not None, "Expected log message not found"
    assert record.levelname == "INFO"
    assert getattr(record, "input_value", None) == "test_log"
    assert getattr(record, "service", None) == service_name
    assert getattr(record, "operation", None) == operation_name
