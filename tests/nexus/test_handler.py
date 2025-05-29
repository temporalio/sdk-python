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


# TODO: type check nexus implementation under mypy

# TODO(dan): test dynamic creation of a service from unsugared definition
# TODO(dan): test malformed caller_links and handler_links

# TODO(dan): test good error message on forgetting to add decorators etc


@nexusrpc.contract.service
class MyService:
    echo: nexusrpc.contract.Operation[Input, Output]
    hang: nexusrpc.contract.Operation[Input, Never]
    log: nexusrpc.contract.Operation[Input, Output]
    async_operation: nexusrpc.contract.Operation[Input, Output]
    async_operation_without_type_annotations: nexusrpc.contract.Operation[Input, Output]
    sync_operation_without_type_annotations: nexusrpc.contract.Operation[Input, Output]
    sync_operation_with_non_async_def: nexusrpc.contract.Operation[Input, Output]
    sync_operation_with_non_async_callable_instance: nexusrpc.contract.Operation[
        Input, Output
    ]
    operation_returning_unwrapped_result_at_runtime_error: nexusrpc.contract.Operation[
        Input, Output
    ]
    non_retryable_application_error: nexusrpc.contract.Operation[Input, Output]
    retryable_application_error: nexusrpc.contract.Operation[Input, Output]
    check_operation_timeout_header: nexusrpc.contract.Operation[Input, Output]
    workflow_run_op_link_test: nexusrpc.contract.Operation[Input, Output]


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from workflow: {input.value}")


@workflow.defn
class WorkflowWithoutTypeAnnotations:
    @workflow.run
    async def run(self, input):  # type: ignore
        return Output(value=f"from workflow without type annotations: {input.value}")  # type: ignore


@workflow.defn
class MyLinkTestWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from link test workflow: {input.value}")


# TODO: implement some of these ops as explicit OperationHandler classes to provide coverage for that?


@nexusrpc.handler.service_handler(service=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation_handler
    async def echo(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        assert ctx.headers["test-header-key"] == "test-header-value"
        ctx.handler_links.extend(ctx.caller_links)
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    @nexusrpc.handler.sync_operation_handler
    async def hang(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Never:
        await asyncio.Future()

    @nexusrpc.handler.sync_operation_handler
    async def non_retryable_application_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        raise ApplicationError(
            "non-retryable application error",
            "details arg",
            # TODO(dan): what values of `type` should be tested?
            type="TestFailureType",
            non_retryable=True,
        )

    @nexusrpc.handler.sync_operation_handler
    async def retryable_application_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        raise ApplicationError(
            "retryable application error",
            "details arg",
            type="TestFailureType",
            non_retryable=False,
        )

    @nexusrpc.handler.sync_operation_handler
    async def handler_error_internal(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        raise nexusrpc.handler.HandlerError(
            message="deliberate internal handler error",
            type=nexusrpc.handler.HandlerErrorType.INTERNAL,
            retryable=False,
            cause=RuntimeError("cause message"),
        )

    @nexusrpc.handler.sync_operation_handler
    async def operation_error_failed(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        raise nexusrpc.handler.OperationError(
            message="deliberate operation error",
            state=nexusrpc.handler.OperationErrorState.FAILED,
        )

    @nexusrpc.handler.sync_operation_handler
    async def check_operation_timeout_header(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        assert "operation-timeout" in ctx.headers
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    @nexusrpc.handler.sync_operation_handler
    async def log(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        logger.info("Logging from start method", extra={"input_value": input.value})
        return Output(value=f"logged: {input.value}")

    @temporalio.nexus.handler.workflow_run_operation_handler
    async def async_operation(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> WorkflowHandle[Any, Output]:
        assert "operation-timeout" in ctx.headers
        return await start_workflow(
            ctx,
            MyWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )

    @nexusrpc.handler.sync_operation_handler
    def sync_operation_with_non_async_def(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> Output:
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    class SyncOperationWithNonAsyncCallableInstance:
        def __call__(
            self,
            _handler: "MyServiceHandler",
            ctx: nexusrpc.handler.StartOperationContext,
            input: Input,
        ) -> Output:
            return Output(
                value=f"from start method on {_handler.__class__.__name__}: {input.value}"
            )

    sync_operation_with_non_async_callable_instance = (
        nexusrpc.handler.sync_operation_handler(
            SyncOperationWithNonAsyncCallableInstance()
        )
    )

    @nexusrpc.handler.sync_operation_handler
    async def sync_operation_without_type_annotations(self, ctx, input):  # type: ignore
        return Output(
            value=f"from start method on {self.__class__.__name__} without type annotations: {input['value']}"  # type: ignore
        )

    @temporalio.nexus.handler.workflow_run_operation_handler
    async def async_operation_without_type_annotations(self, ctx, input):  # type: ignore
        return await start_workflow(
            ctx,
            WorkflowWithoutTypeAnnotations.run,
            input,
            id=str(uuid.uuid4()),
        )

    @temporalio.nexus.handler.workflow_run_operation_handler
    async def workflow_run_op_link_test(
        self, ctx: nexusrpc.handler.StartOperationContext, input: Input
    ) -> WorkflowHandle[Any, Output]:
        assert any(
            link.url == "http://caller-link/" for link in ctx.caller_links
        ), "Caller link not found"
        assert ctx.request_id == "test-request-id-123", "Request ID mismatch"
        ctx.handler_links.extend(ctx.caller_links)
        return await start_workflow(
            ctx,
            MyLinkTestWorkflow.run,
            input,
            id=f"link-test-{uuid.uuid4()}",
        )

    class OperationHandlerReturningUnwrappedResult(
        nexusrpc.handler.OperationHandler[Input, Output]
    ):
        async def start(
            self,
            ctx: nexusrpc.handler.StartOperationContext,
            input: Input,
            # This return type is a type error, but VSCode doesn't flag it unless
            # "python.analysis.typeCheckingMode" is set to "strict"
        ) -> Output:
            # Invalid: start method must wrap result as StartOperationResultSync
            # or StartOperationResultAsync
            return Output(value="unwrapped result error")  # type: ignore

    @nexusrpc.handler.operation_handler
    def operation_returning_unwrapped_result_at_runtime_error(
        self,
    ) -> nexusrpc.handler.OperationHandler[Input, Output]:
        return MyServiceHandler.OperationHandlerReturningUnwrappedResult()


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
    service: str = MyService.__name__
    input: Input = Input("")
    headers: dict[str, str] = {}
    expected: SuccessfulResponse
    skip = ""

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        assert response.status_code == cls.expected.status_code, (
            f"expected status code {cls.expected.status_code} "
            f"but got {response.status_code} for response content {response.content.decode()}"
        )
        if cls.expected.body_json is not None:
            body = response.json()
            assert isinstance(body, dict)
            if isinstance(cls.expected.body_json, dict):
                assert body == cls.expected.body_json
            else:
                assert cls.expected.body_json(body)
        assert response.headers.items() >= cls.expected.headers.items()


class _FailureTestCase(_TestCase):
    expected: UnsuccessfulResponse

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        super().check_response(response)
        failure = Failure(**response.json())

        if isinstance(cls.expected.failure_message, str):
            assert failure.message == cls.expected.failure_message
        else:
            assert cls.expected.failure_message(failure.message)

        # retryability assertions
        if (
            retryable_header := response.headers.get("nexus-request-retryable")
        ) is not None:
            assert json.loads(retryable_header) == cls.expected.retryable_header
        else:
            assert cls.expected.retryable_header is None

        if failure.exception:
            assert isinstance(failure.exception, ApplicationError)
            assert failure.exception.retryable == cls.expected.retryable_exception
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
    expected = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method on MyServiceHandler: hello"},
    )
    # TODO(dan): headers should be lower-cased
    assert (
        headers.get("Nexus-Link") == '<http://test/>; type="test"'
    ), "Nexus-Link header not echoed correctly."


class SyncHandlerHappyPathNonAsyncDef(_TestCase):
    skip = "Non-async def executor not implemented"
    operation = "sync_operation_with_non_async_def"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method on MyServiceHandler: hello"},
    )


class SyncHandlerHappyPathWithNonAsyncCallableInstance(_TestCase):
    operation = "sync_operation_with_non_async_callable_instance"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method on MyServiceHandler: hello"},
    )


class SyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "sync_operation_without_type_annotations"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=200,
        body_json={
            "value": "from start method on MyServiceHandler without type annotations: hello"
        },
    )


class AsyncHandlerHappyPath(_TestCase):
    operation = "async_operation"
    input = Input("hello")
    headers = {"Operation-Timeout": "777s"}
    expected = SuccessfulResponse(
        status_code=201,
    )


class AsyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "async_operation_without_type_annotations"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=201,
    )


class WorkflowRunOpLinkTestHappyPath(_TestCase):
    # TODO(dan): fix this test
    skip = "Yields invalid link"
    operation = "workflow_run_op_link_test"
    input = Input("link-test-input")
    headers = {
        "Nexus-Link": '<http://caller-link/>; type="test"',
        "Nexus-Request-Id": "test-request-id-123",
    }
    expected = SuccessfulResponse(
        status_code=201,
    )

    @classmethod
    def check_response(cls, response: httpx.Response) -> None:
        super().check_response(response)
        nexus_link = response.headers.get("nexus-link")
        assert nexus_link is not None, "nexus-link header not found in response"
        assert nexus_link.startswith(
            "<temporal://"
        ), f"nexus-link header {nexus_link} does not start with <temporal://"


# TODO(dan): Before fixing the upstream-timeout test by implementing the handler for the
# timeout cancellation sent by core, I was seeing 2025-05-11T22:41:51.853243Z  WARN
# temporal_sdk_core::worker::nexus: Failed to parse nexus timeout header value
# '5.617792ms'

# TODO(dan): test Nexus-Callback- headers
# TODO(dan): make assertions about failure.exception.cause


class OperationHandlerReturningUnwrappedResultError(_FailureTestCase):
    operation = "operation_returning_unwrapped_result_at_runtime_error"
    expected = UnsuccessfulResponse(
        status_code=500,
        retryable_header=False,
        failure_message=(
            "Operation start method must return either "
            "nexusrpc.handler.StartOperationResultSync or nexusrpc.handler.StartOperationResultAsync"
        ),
    )


class UpstreamTimeoutViaRequestTimeout(_FailureTestCase):
    operation = "hang"
    headers = {"Request-Timeout": "10ms"}
    expected = UnsuccessfulResponse(
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
    expected = SuccessfulResponse(
        status_code=200,
    )


class BadRequest(_FailureTestCase):
    operation = "echo"
    input = Input(7)  # type: ignore
    expected = UnsuccessfulResponse(
        status_code=400,
        retryable_header=False,
        failure_message=lambda s: s.startswith("Failed converting field"),
    )


class NonRetryableApplicationError(_FailureTestCase):
    operation = "non_retryable_application_error"
    expected = UnsuccessfulResponse(
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
    expected = UnsuccessfulResponse(
        status_code=500,
        retryable_header=True,
        failure_message="retryable application error",
    )


class HandlerErrorInternal(_FailureTestCase):
    operation = "handler_error_internal"
    expected = UnsuccessfulResponse(
        status_code=500,
        # TODO(dan): check this assertion
        retryable_header=False,
        failure_message="cause message",
    )


class OperationError(_FailureTestCase):
    operation = "operation_error_failed"
    expected = UnsuccessfulResponse(
        status_code=424,
        # TODO(dan): check that OperationError should not set retryable header
        retryable_header=None,
        failure_message="deliberate operation error",
        headers=UNSUCCESSFUL_RESPONSE_HEADERS | {"nexus-operation-state": "failed"},
    )


class UnregisteredService(_FailureTestCase):
    operation = "echo"
    service = "NonExistentService"
    expected = UnsuccessfulResponse(
        status_code=404,
        retryable_header=False,
        failure_message="Nexus service 'NonExistentService' has not been registered.",
    )


class UnregisteredOperation(_FailureTestCase):
    operation = "NonExistentOperation"
    expected = UnsuccessfulResponse(
        status_code=404,
        retryable_header=False,
        failure_message="Nexus service 'MyService' has no operation 'NonExistentOperation'.",
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SyncHandlerHappyPath,
        SyncHandlerHappyPathNonAsyncDef,
        # TODO(dan): make callable instance work
        # SyncHandlerHappyPathWithNonAsyncCallableInstance,
        SyncHandlerHappyPathWithoutTypeAnnotations,
        AsyncHandlerHappyPath,
        AsyncHandlerHappyPathWithoutTypeAnnotations,
        WorkflowRunOpLinkTestHappyPath,
    ],
)
async def test_start_operation_happy_path(test_case: Type[_TestCase], client: Client):
    await _test_start_operation(test_case, client)


@pytest.mark.parametrize(
    "test_case",
    [
        OperationHandlerReturningUnwrappedResultError,
        UpstreamTimeoutViaRequestTimeout,
        OperationTimeoutHeader,
        BadRequest,
        HandlerErrorInternal,
        UnregisteredService,
        UnregisteredOperation,
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
    endpoint = (await create_nexus_endpoint(task_queue, client)).endpoint.id
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_services=[MyServiceHandler()],
    ):
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"http://127.0.0.1:{HTTP_PORT}/nexus/endpoints/{endpoint}/services/{test_case.service}/{test_case.operation}",
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
