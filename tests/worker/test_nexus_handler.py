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
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Type, Union

import httpx
import nexusrpc
import nexusrpc.handler
import pytest
from google.protobuf import json_format
from hyperlinked import print
from typing_extensions import Never

import temporalio.api.failure.v1
import temporalio.nexus
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.converter import FailureConverter, PayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.nexus import logger
from temporalio.nexus.handler import start_workflow
from temporalio.worker import Worker
from tests.helpers.nexus import create_nexus_endpoint

HTTP_PORT = 7243


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
    hang: nexusrpc.interface.Operation[Input, Never]
    log: nexusrpc.interface.Operation[Input, Output]
    async_operation: nexusrpc.interface.Operation[Input, Output]
    async_operation_without_type_annotations: nexusrpc.interface.Operation[
        Input, Output
    ]
    sync_operation_without_type_annotations: nexusrpc.interface.Operation[Input, Output]
    non_retryable_application_error: nexusrpc.interface.Operation[Input, Output]
    retryable_application_error: nexusrpc.interface.Operation[Input, Output]
    check_operation_timeout_header: nexusrpc.interface.Operation[Input, Output]


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from workflow: {input.value}")


@workflow.defn
class WorkflowWithoutTypeAnnotations:
    @workflow.run
    async def run(self, input):
        return Output(value=f"from workflow without type annotations: {input.value}")


@nexusrpc.handler.service(interface=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation
    async def echo(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        assert options.headers["test-header-key"] == "test-header-value"
        return Output(value=f"from start method: {input.value}")

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
    async def check_operation_timeout_header(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        assert "operation-timeout" in options.headers
        return Output(value=f"from start method: {input.value}")

    @nexusrpc.handler.sync_operation
    async def log(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> Output:
        logger.info("Logging from start method", extra={"input_value": input.value})
        return Output(value=f"logged: {input.value}")

    @temporalio.nexus.handler.workflow_run_operation
    async def async_operation(
        self, input: Input, options: nexusrpc.handler.StartOperationOptions
    ) -> WorkflowHandle[Any, Output]:
        assert "operation-timeout" in options.headers
        return await start_workflow(
            MyWorkflow.run,
            input,
            id=str(uuid.uuid4()),
            options=options,
        )

    @nexusrpc.handler.sync_operation
    async def sync_operation_without_type_annotations(self, input, options):
        return Output(
            value=f"from start method without type annotations: {input['value']}"  # type: ignore
        )

    @temporalio.nexus.handler.workflow_run_operation
    async def async_operation_without_type_annotations(self, input, options):
        return await start_workflow(
            WorkflowWithoutTypeAnnotations.run,
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
        # TODO(dan): every exception in the chain is being rehydrated wrapped in ApplicationError.
        # Is this the intended design, or is it an error in the way I am rehydrating exceptions
        # here? I.e., should the Python SDK unwrap ApplicationErrors to produce an error chain of
        # native Python exception types?
        proto = {
            "temporal.api.failure.v1.Failure": temporalio.api.failure.v1.Failure,
        }[error_type]()
        json_format.ParseDict(self.details, proto, ignore_unknown_fields=True)
        return FailureConverter.default.from_failure(proto, PayloadConverter.default)


# Immutable dicts that can be used as dataclass field defaults

SUCCESSFUL_RESPONSE_HEADERS = MappingProxyType(
    {
        "content-type": "application/json",
    }
)

UNSUCCESSFUL_RESPONSE_HEADERS = MappingProxyType(
    {
        "content-type": "application/json",
        "temporal-nexus-failure-source": "worker",
    }
)


@dataclass
class SuccessfulResponse:
    status_code: int
    body_json: Optional[Union[dict[str, Any], Callable[[dict[str, Any]], bool]]] = None
    headers: Mapping[str, str] = SUCCESSFUL_RESPONSE_HEADERS


@dataclass
class UnsuccessfulResponse:
    status_code: int
    # Expected value of Nexus-Request-Retryable header
    retryable_header: Optional[bool]
    failure_message: Union[str, Callable[[str], bool]]
    # Expected value of inverse of non_retryable attribute of exception.
    retryable_exception: bool = True
    # TODO(dan): the body of a successful response need not be JSON; test non-JSON-parseable string
    body_json: Optional[Callable[[dict[str, Any]], bool]] = None
    headers: Mapping[str, str] = UNSUCCESSFUL_RESPONSE_HEADERS


class _TestCase:
    operation: str
    input: Input = Input("")
    headers: dict[str, str] = {}
    expected_response: SuccessfulResponse
    skip = ""

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        assert response.status_code == cls.expected_response.status_code, (
            f"expected status code {cls.expected_response.status_code} "
            f"but got {response.status_code} for response content {response.content.decode()}"
        )
        if cls.expected_response.body_json is not None:
            body = response.json()
            assert isinstance(body, dict)
            if isinstance(cls.expected_response.body_json, dict):
                assert body == cls.expected_response.body_json
            else:
                assert cls.expected_response.body_json(body)
        if cls.expected_response.headers is not None:
            assert response.headers.items() >= cls.expected_response.headers.items()


class _FailureTestCase(_TestCase):
    expected_response: UnsuccessfulResponse

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        super().check_response(response)
        failure = Failure(**response.json())

        if isinstance(cls.expected_response.failure_message, str):
            assert failure.message == cls.expected_response.failure_message
        else:
            assert cls.expected_response.failure_message(failure.message)

        # retryability assertions
        if (
            retryable_header := response.headers.get("nexus-request-retryable")
        ) is not None:
            assert (
                json.loads(retryable_header) == cls.expected_response.retryable_header
            )
        else:
            assert cls.expected_response.retryable_header is None

        if failure.exception:
            assert isinstance(failure.exception, ApplicationError)
            assert (
                failure.exception.retryable == cls.expected_response.retryable_exception
            )
        else:
            print(f"TODO(dan): {cls} did not yield a Failure with exception details")


class SyncHandlerHappyPath(_TestCase):
    operation = "echo"
    input = Input("hello")
    # TODO(dan): why is application/json randomly scattered around these tests?
    headers = {
        "Content-Type": "application/json",
        "Test-Header-Key": "test-header-value",
        "Nexus-Link": '<http://test/>; type="test"',
    }
    expected_response = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method: hello"},
    )
    # TODO(dan): Support manually adding links in operation handler
    # See e.g. TS nexus.handlerLinks().push(...options.links)
    # assert headers.get("nexus-link") == "<http://test/>; type=\"test\"", \
    #     "Nexus-Link header not echoed correctly."


class SyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "sync_operation_without_type_annotations"
    input = Input("hello")
    expected_response = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method without type annotations: hello"},
    )


class AsyncHandlerHappyPath(_TestCase):
    operation = "async_operation"
    input = Input("hello")
    headers = {"Operation-Timeout": "777s"}
    expected_response = SuccessfulResponse(
        status_code=201,
    )


class AsyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "async_operation_without_type_annotations"
    input = Input("hello")
    expected_response = SuccessfulResponse(
        status_code=201,
    )


# TODO(dan): Before fixing the upstream-timeout test by implementing the handler for the
# timeout cancellation sent by core, I was seeing 2025-05-11T22:41:51.853243Z  WARN
# temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value
# '5.617792ms'

# TODO(dan): test Nexus-Callback- headers
# TODO(dan): make assertions about failure.exception.cause


class UpstreamTimeoutViaRequestTimeout(_FailureTestCase):
    operation = "hang"
    headers = {"Request-Timeout": "10ms"}
    expected_response = UnsuccessfulResponse(
        status_code=520,
        # TODO(dan): should this have the retryable header set?
        retryable_header=None,
        # This error is returned by the server; it doesn't populate metadata or details, and it
        # doesn't set temporal-nexus-failure-source.
        failure_message="upstream timeout",
        headers={
            "content-type": "application/json",
        },
    )


class OperationTimeoutHeader(_TestCase):
    # The only assertion we make in the _handler_ test suite is that the header is present. The
    # handler does not currently do anything with this header; the user may use it to configure
    # timeouts on executions they start in their handler.
    operation = "check_operation_timeout_header"
    headers = {"Operation-Timeout": "10ms"}
    expected_response = SuccessfulResponse(
        status_code=200,
    )


class BadRequest(_FailureTestCase):
    operation = "echo"
    input = Input(7)  # type: ignore
    expected_response = UnsuccessfulResponse(
        status_code=400,
        retryable_header=False,
        failure_message=lambda s: s.startswith("Failed converting field"),
    )


class NonRetryableApplicationError(_FailureTestCase):
    operation = "non_retryable_application_error"
    expected_response = UnsuccessfulResponse(
        status_code=500,
        retryable_header=False,
        retryable_exception=False,
        failure_message="non-retryable application error",
    )

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        super().check_response(response)
        failure = Failure(**response.json())
        err = failure.exception
        assert isinstance(err, ApplicationError)
        assert err.non_retryable
        assert err.type == "TestFailureType"
        assert err.details == ("details arg",)


class RetryableApplicationError(_FailureTestCase):
    operation = "retryable_application_error"
    expected_response = UnsuccessfulResponse(
        status_code=500,
        retryable_header=True,
        failure_message="retryable application error",
    )


class HandlerErrorInternal(_FailureTestCase):
    operation = "handler_error_internal"
    expected_response = UnsuccessfulResponse(
        status_code=500,
        # TODO(dan): check this assertion
        retryable_header=False,
        failure_message="cause message",
    )


class OperationError(_FailureTestCase):
    operation = "operation_error_failed"
    expected_response = UnsuccessfulResponse(
        status_code=424,
        # TODO(dan): check that OperationError should not set retryable header
        retryable_header=None,
        failure_message="deliberate operation error",
        headers=UNSUCCESSFUL_RESPONSE_HEADERS | {"nexus-operation-state": "failed"},
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SyncHandlerHappyPath,
        SyncHandlerHappyPathWithoutTypeAnnotations,
        AsyncHandlerHappyPath,
        AsyncHandlerHappyPathWithoutTypeAnnotations,
    ],
)
async def test_start_operation_happy_path(test_case: Type[_TestCase], client: Client):
    await _test_start_operation(test_case, client)


@pytest.mark.parametrize(
    "test_case",
    [
        UpstreamTimeoutViaRequestTimeout,
        OperationTimeoutHeader,
        BadRequest,
        HandlerErrorInternal,
    ],
)
async def test_start_operation_protocol_level_failures(
    test_case: Type[_TestCase], client: Client
):
    await _test_start_operation(test_case, client)


@pytest.mark.parametrize(
    "test_case",
    [
        NonRetryableApplicationError,
        RetryableApplicationError,
        OperationError,
    ],
)
async def test_start_operation_operation_failures(
    test_case: Type[_TestCase], client: Client
):
    await _test_start_operation(test_case, client)


async def _test_start_operation(test_case: Type[_TestCase], client: Client):
    if test_case.skip:
        pytest.skip(test_case.skip)
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
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service}/{test_case.operation}",
                json=dataclass_as_dict(test_case.input),
                headers=test_case.headers,
            )
            test_case.check_response(response)

    print(
        "\n\n----------------------------------------------------------------------\n\n"
    )


async def test_logger_uses_operation_context(client: Client, caplog: Any):
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
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{service_name}/{operation_name}",
                json=dataclass_as_dict(Input("test_log")),
                # TODO(dan): why these headers?
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
            and record.getMessage() == "Logging from start method"
        ),
        None,
    )
    assert record is not None, "Expected log message not found"
    assert record.levelname == "INFO"
    assert getattr(record, "input_value", None) == "test_log"
    assert getattr(record, "service", None) == service_name
    assert getattr(record, "operation", None) == operation_name


def dataclass_as_dict(dataclass: Any) -> dict[str, Any]:
    """
    Return a shallow dict of the dataclass's fields.

    dataclasses.as_dict goes too far (attempts to pickle values)
    """
    return {
        field.name: getattr(dataclass, field.name)
        for field in dataclasses.fields(dataclass)
    }
