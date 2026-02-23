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
import concurrent.futures
import logging
import pprint
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx
import nexusrpc
import pytest
from nexusrpc import (
    HandlerError,
    HandlerErrorType,
    OperationError,
    OperationErrorState,
)
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultSync,
    service_handler,
    sync_operation,
)
from nexusrpc.handler._decorators import operation_handler

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.nexus import (
    WorkflowRunOperationContext,
    workflow_run_operation,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import (
    Failure,
    ServiceClient,
    dataclass_as_dict,
    make_nexus_endpoint_name,
)


@dataclass
class Input:
    value: str


@dataclass
class Output:
    value: str


@dataclass
class NonSerializableOutput:
    callable: Callable[[], Any] = lambda: None


# TODO(nexus-prelease): Test attaching multiple callers to the same operation.
# TODO(nexus-preview): type check nexus implementation under mypy
# TODO(nexus-preview): test malformed inbound_links and outbound_links


@nexusrpc.service
class MyService:
    echo: nexusrpc.Operation[Input, Output]
    echo_renamed: nexusrpc.Operation[Input, Output] = nexusrpc.Operation(
        name="echo-renamed"
    )
    hang: nexusrpc.Operation[Input, Output]
    log: nexusrpc.Operation[Input, Output]
    workflow_run_operation_happy_path: nexusrpc.Operation[Input, Output]
    sync_operation_with_non_async_def: nexusrpc.Operation[Input, Output]
    operation_returning_unwrapped_result_at_runtime_error: nexusrpc.Operation[
        Input, Output
    ]
    non_retryable_application_error: nexusrpc.Operation[Input, Output]
    retryable_application_error: nexusrpc.Operation[Input, Output]
    check_operation_timeout_header: nexusrpc.Operation[Input, Output]
    workflow_run_op_link_test: nexusrpc.Operation[Input, Output]
    handler_error_internal: nexusrpc.Operation[Input, Output]
    operation_error_failed: nexusrpc.Operation[Input, Output]
    idempotency_check: nexusrpc.Operation[None, Output]
    non_serializable_output: nexusrpc.Operation[Input, NonSerializableOutput]


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from workflow: {input.value}")


@workflow.defn
class WorkflowWithoutTypeAnnotations:
    @workflow.run
    async def run(self, input):  # type: ignore
        return Output(value=f"from workflow without type annotations: {input}")


@workflow.defn
class MyLinkTestWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=f"from link test workflow: {input.value}")


# The service_handler decorator is applied by the test
class MyServiceHandler:
    @sync_operation
    async def echo(self, ctx: StartOperationContext, input: Input) -> Output:
        assert ctx.headers["test-header-key"] == "test-header-value"
        ctx.outbound_links.extend(ctx.inbound_links)
        assert nexus.in_operation()
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    # The name override is present in the service definition. But the test below submits
    # the same operation name in the request whether using a service definition or now.
    # The name override here is necessary when the test is not using the service
    # definition. It should be permitted when the service definition is in effect, as
    # long as the name override is the same as that in the service definition.
    @sync_operation(name="echo-renamed")
    async def echo_renamed(self, ctx: StartOperationContext, input: Input) -> Output:
        return await self.echo(ctx, input)

    @sync_operation
    async def hang(self, _ctx: StartOperationContext, _input: Input) -> Output:
        await asyncio.Future()
        return Output(value="won't reach here")

    @sync_operation
    async def non_retryable_application_error(
        self, _ctx: StartOperationContext, _input: Input
    ) -> Output:
        raise ApplicationError(
            "non-retryable application error",
            "details arg",
            # TODO(nexus-preview): what values of `type` should be tested?
            type="TestFailureType",
            non_retryable=True,
        )

    @sync_operation
    async def retryable_application_error(
        self, _ctx: StartOperationContext, _input: Input
    ) -> Output:
        raise ApplicationError(
            "retryable application error",
            "details arg",
            type="TestFailureType",
            non_retryable=False,
        )

    @sync_operation
    async def handler_error_internal(
        self, _ctx: StartOperationContext, _input: Input
    ) -> Output:
        raise HandlerError(
            message="deliberate internal handler error",
            type=HandlerErrorType.INTERNAL,
            retryable_override=False,
        ) from RuntimeError("cause message")

    @sync_operation
    async def operation_error_failed(
        self, _ctx: StartOperationContext, _input: Input
    ) -> Output:
        raise OperationError(
            message="deliberate operation error",
            state=OperationErrorState.FAILED,
        )

    @sync_operation
    async def check_operation_timeout_header(
        self, ctx: StartOperationContext, input: Input
    ) -> Output:
        assert "operation-timeout" in ctx.headers
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    @sync_operation
    async def log(self, _ctx: StartOperationContext, input: Input) -> Output:
        nexus.logger.info(
            "Logging from start method", extra={"input_value": input.value}
        )
        return Output(value=f"logged: {input.value}")

    @workflow_run_operation
    async def workflow_run_operation_happy_path(
        self, ctx: WorkflowRunOperationContext, input: Input
    ) -> nexus.WorkflowHandle[Output]:
        assert nexus.in_operation()
        return await ctx.start_workflow(
            MyWorkflow.run,
            input,
            id=str(uuid.uuid4()),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    @sync_operation
    async def sync_operation_with_non_async_def(
        self, _ctx: StartOperationContext, input: Input
    ) -> Output:
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )

    @workflow_run_operation
    async def workflow_run_op_link_test(
        self, ctx: WorkflowRunOperationContext, input: Input
    ) -> nexus.WorkflowHandle[Output]:
        assert any(
            link.url == "http://inbound-link/" for link in ctx.inbound_links
        ), "Inbound link not found"
        assert ctx.request_id == "test-request-id-123", "Request ID mismatch"
        ctx.outbound_links.extend(ctx.inbound_links)

        return await ctx.start_workflow(
            MyLinkTestWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )

    class OperationHandlerReturningUnwrappedResult(OperationHandler[Input, Output]):
        async def start(  # type: ignore[override] # intentional test error
            self,
            ctx: StartOperationContext,
            input: Input,
            # This return type is a type error, but VSCode doesn't flag it unless
            # "python.analysis.typeCheckingMode" is set to "strict"
        ) -> Output:
            # Invalid: start method must wrap result as StartOperationResultSync
            # or StartOperationResultAsync
            return Output(value="unwrapped result error")

        async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
            raise NotImplementedError

    @operation_handler
    def operation_returning_unwrapped_result_at_runtime_error(
        self,
    ) -> OperationHandler[Input, Output]:
        return MyServiceHandler.OperationHandlerReturningUnwrappedResult()

    @sync_operation
    async def idempotency_check(
        self, ctx: StartOperationContext, _input: None
    ) -> Output:
        return Output(value=f"request_id: {ctx.request_id}")

    @sync_operation
    async def non_serializable_output(
        self, _ctx: StartOperationContext, _input: Input
    ) -> NonSerializableOutput:
        return NonSerializableOutput()


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
    body_json: dict[str, Any] | Callable[[dict[str, Any]], bool] | None = None
    headers: Mapping[str, str] = field(
        default_factory=lambda: SUCCESSFUL_RESPONSE_HEADERS
    )


@dataclass
class UnsuccessfulResponse:
    status_code: int
    failure_message: str | Callable[[str], bool]
    # Is the Nexus Failure expected to have the details field populated?
    failure_details: bool = True
    # Expected value of inverse of non_retryable attribute of exception.
    retryable_exception: bool = True
    body_json: Callable[[dict[str, Any]], bool] | None = None
    headers: Mapping[str, str] = field(
        default_factory=lambda: UNSUCCESSFUL_RESPONSE_HEADERS
    )


class _TestCase:
    operation: str  # type:ignore[reportUninitializedInstanceVariable]
    service_defn: str = "MyService"
    input: Input = Input("")
    headers: dict[str, str] = {}
    expected: SuccessfulResponse  # type:ignore[reportUninitializedInstanceVariable]
    expected_without_service_definition: SuccessfulResponse | None = None
    skip = ""

    @classmethod
    def check_response(
        cls,
        response: httpx.Response,
        with_service_definition: bool,
    ) -> None:
        assert response.status_code == cls.expected.status_code, (
            f"expected status code {cls.expected.status_code} "
            f"but got {response.status_code} for response content"
            f"{pprint.pformat(response.content.decode())}"
        )
        if not with_service_definition and cls.expected_without_service_definition:
            expected = cls.expected_without_service_definition
        else:
            expected = cls.expected
        if expected.body_json is not None:
            body = response.json()
            assert isinstance(body, dict)
            if isinstance(expected.body_json, dict):
                assert body == expected.body_json
            else:
                assert expected.body_json(body)
        assert response.headers.items() >= cls.expected.headers.items()


class _FailureTestCase(_TestCase):
    expected: UnsuccessfulResponse  # type: ignore[assignment]

    @classmethod
    def check_response(
        cls, response: httpx.Response, with_service_definition: bool
    ) -> None:
        super().check_response(response, with_service_definition)
        failure = Failure(**response.json())

        if isinstance(cls.expected.failure_message, str):
            assert failure.message == cls.expected.failure_message
        else:
            assert cls.expected.failure_message(failure.message)


class SyncHandlerHappyPath(_TestCase):
    operation = "echo"
    input = Input("hello")
    # TODO(nexus-prerelease): why is application/json randomly scattered around these tests?
    headers = {
        "Content-Type": "application/json",
        "Test-Header-Key": "test-header-value",
        "Nexus-Link": '<http://test/>; type="test"',
    }
    expected = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method on MyServiceHandler: hello"},
    )
    # TODO(nexus-prerelease): headers should be lower-cased
    assert (
        headers.get("Nexus-Link") == '<http://test/>; type="test"'
    ), "Nexus-Link header not echoed correctly."


class SyncHandlerHappyPathRenamed(SyncHandlerHappyPath):
    operation = "echo-renamed"


class SyncHandlerHappyPathNonAsyncDef(_TestCase):
    operation = "sync_operation_with_non_async_def"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=200,
        body_json={"value": "from start method on MyServiceHandler: hello"},
    )


class AsyncHandlerHappyPath(_TestCase):
    operation = "workflow_run_operation_happy_path"
    input = Input("hello")
    headers = {"Operation-Timeout": "777s"}
    expected = SuccessfulResponse(
        status_code=201,
    )


class WorkflowRunOpLinkTestHappyPath(_TestCase):
    # TODO(nexus-prerelease): fix this test
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
    def check_response(
        cls, response: httpx.Response, with_service_definition: bool
    ) -> None:
        super().check_response(response, with_service_definition)
        nexus_link = response.headers.get("nexus-link")
        assert nexus_link is not None, "nexus-link header not found in response"
        assert nexus_link.startswith(
            "<temporal://"
        ), f"nexus-link header {nexus_link} does not start with <temporal://"


# TODO(nexus-prerelease): test Nexus-Callback- headers
# TODO(nexus-prerelease): make assertions about failure.exception.cause


class OperationHandlerReturningUnwrappedResultError(_FailureTestCase):
    operation = "operation_returning_unwrapped_result_at_runtime_error"
    expected = UnsuccessfulResponse(
        status_code=500,
        failure_message=(
            "Operation start method must return either "
            "nexusrpc.handler.StartOperationResultSync or "
            "nexusrpc.handler.StartOperationResultAsync."
        ),
    )


class UpstreamTimeoutViaRequestTimeout(_FailureTestCase):
    operation = "hang"
    headers = {"Request-Timeout": "10ms"}
    expected = UnsuccessfulResponse(
        status_code=520,
        # This error is returned by the server; it doesn't populate metadata or details, and it
        # doesn't set temporal-nexus-failure-source.
        failure_details=False,
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
        failure_message=lambda s: s.startswith(
            "Data converter failed to decode Nexus operation input"
        ),
    )


class _ApplicationErrorTestCase(_FailureTestCase):
    """Test cases in which the operation raises an ApplicationError."""

    expected: UnsuccessfulResponse  # type: ignore[assignment]


class NonRetryableApplicationError(_ApplicationErrorTestCase):
    operation = "non_retryable_application_error"
    expected = UnsuccessfulResponse(
        status_code=500,
        retryable_exception=False,
        failure_message="non-retryable application error",
    )


class RetryableApplicationError(_ApplicationErrorTestCase):
    operation = "retryable_application_error"
    expected = UnsuccessfulResponse(
        status_code=500,
        failure_message="retryable application error",
    )


class HandlerErrorInternal(_FailureTestCase):
    operation = "handler_error_internal"
    expected = UnsuccessfulResponse(
        status_code=500,
        failure_message="deliberate internal handler error",
    )


class OperationErrorFailed(_FailureTestCase):
    operation = "operation_error_failed"
    expected = UnsuccessfulResponse(
        status_code=424,
        failure_message="deliberate operation error",
        headers=UNSUCCESSFUL_RESPONSE_HEADERS | {"nexus-operation-state": "failed"},
    )


class NonSerializableOutputFailure(_FailureTestCase):
    operation = "non_serializable_output"
    expected = UnsuccessfulResponse(
        status_code=500,
        failure_message="Object of type function is not JSON serializable",
    )


@pytest.mark.parametrize(
    "test_case",
    [
        SyncHandlerHappyPath,
        SyncHandlerHappyPathRenamed,
        SyncHandlerHappyPathNonAsyncDef,
        AsyncHandlerHappyPath,
        WorkflowRunOpLinkTestHappyPath,
    ],
)
@pytest.mark.parametrize("with_service_definition", [True, False])
async def test_start_operation_happy_path(
    test_case: type[_TestCase],
    with_service_definition: bool,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    if with_service_definition:
        await _test_start_operation_with_service_definition(test_case, env)
    else:
        await _test_start_operation_without_service_definition(test_case, env)


@pytest.mark.parametrize(
    "test_case",
    [
        OperationHandlerReturningUnwrappedResultError,
        UpstreamTimeoutViaRequestTimeout,
        OperationTimeoutHeader,
        BadRequest,
        HandlerErrorInternal,
        NonSerializableOutputFailure,
    ],
)
async def test_start_operation_protocol_level_failures(
    test_case: type[_TestCase], env: WorkflowEnvironment
):
    if test_case == UpstreamTimeoutViaRequestTimeout:
        pytest.skip(
            "TODO(nexus-preview): UpstreamTimeoutViaRequestTimeout test is flaky"
        )

    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    await _test_start_operation_with_service_definition(test_case, env)


@pytest.mark.parametrize(
    "test_case",
    [
        NonRetryableApplicationError,
        RetryableApplicationError,
        OperationErrorFailed,
    ],
)
async def test_start_operation_operation_failures(
    test_case: type[_TestCase], env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    await _test_start_operation_with_service_definition(test_case, env)


async def _test_start_operation_with_service_definition(
    test_case: type[_TestCase],
    env: WorkflowEnvironment,
):
    if test_case.skip:
        pytest.skip(test_case.skip)
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=(test_case.service_defn),
    )

    with pytest.WarningsRecorder() as warnings:
        decorator = service_handler(service=MyService)
        user_service_handler = decorator(MyServiceHandler)()

        async with Worker(
            env.client,
            task_queue=task_queue,
            nexus_service_handlers=[user_service_handler],
            nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
        ):
            response = await service_client.start_operation(
                test_case.operation,
                dataclass_as_dict(test_case.input),
                test_case.headers,
            )
            test_case.check_response(response, with_service_definition=True)

    assert not any(warnings), [w.message for w in warnings]


async def _test_start_operation_without_service_definition(
    test_case: type[_TestCase],
    env: WorkflowEnvironment,
):
    if test_case.skip:
        pytest.skip(test_case.skip)
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=MyServiceHandler.__name__,
    )

    with pytest.WarningsRecorder() as warnings:
        decorator = service_handler
        user_service_handler = decorator(MyServiceHandler)()

        async with Worker(
            env.client,
            task_queue=task_queue,
            nexus_service_handlers=[user_service_handler],
            nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
        ):
            response = await service_client.start_operation(
                test_case.operation,
                dataclass_as_dict(test_case.input),
                test_case.headers,
            )
            test_case.check_response(response, with_service_definition=False)

    assert not any(warnings), [w.message for w in warnings]


@nexusrpc.service
class MyServiceWithOperationsWithoutTypeAnnotations:
    workflow_run_operation_without_type_annotations: nexusrpc.Operation[Input, Output]
    sync_operation_without_type_annotations: nexusrpc.Operation[Input, Output]


class MyServiceHandlerWithOperationsWithoutTypeAnnotations:
    @sync_operation
    async def sync_operation_without_type_annotations(self, ctx, input):  # type:ignore[reportMissingParameterType]
        # Despite the lack of type annotations, the input type from the op definition in
        # the service definition is used to deserialize the input.
        return Output(
            value=f"from start method on {self.__class__.__name__} without type annotations: {input}"
        )

    @workflow_run_operation
    async def workflow_run_operation_without_type_annotations(self, ctx, input):  # type:ignore[reportMissingParameterType]
        return await ctx.start_workflow(
            WorkflowWithoutTypeAnnotations.run,
            input,
            id=str(uuid.uuid4()),
        )


class SyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "sync_operation_without_type_annotations"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=200,
        body_json={
            "value": "from start method on MyServiceHandlerWithOperationsWithoutTypeAnnotations without type annotations: Input(value='hello')"
        },
    )


class AsyncHandlerHappyPathWithoutTypeAnnotations(_TestCase):
    operation = "workflow_run_operation_without_type_annotations"
    input = Input("hello")
    expected = SuccessfulResponse(
        status_code=201,
    )


# Attempting to use the service_handler decorator on a class containing an operation
# without type annotations is a validation error (test coverage in nexusrpc)
@pytest.mark.parametrize(
    "test_case",
    [
        SyncHandlerHappyPathWithoutTypeAnnotations,
        AsyncHandlerHappyPathWithoutTypeAnnotations,
    ],
)
async def test_start_operation_without_type_annotations(
    test_case: type[_TestCase], env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    if test_case.skip:
        pytest.skip(test_case.skip)
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=MyServiceWithOperationsWithoutTypeAnnotations.__name__,
    )

    with pytest.WarningsRecorder() as warnings:
        decorator = service_handler(
            service=MyServiceWithOperationsWithoutTypeAnnotations
        )
        user_service_handler = decorator(
            MyServiceHandlerWithOperationsWithoutTypeAnnotations
        )()

        async with Worker(
            env.client,
            task_queue=task_queue,
            nexus_service_handlers=[user_service_handler],
            nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
        ):
            response = await service_client.start_operation(
                test_case.operation,
                dataclass_as_dict(test_case.input),
                test_case.headers,
            )
            test_case.check_response(response, with_service_definition=True)

    assert not any(warnings), [w.message for w in warnings]


def test_operation_without_type_annotations_without_service_definition_raises_validation_error():
    with pytest.raises(
        ValueError,
        match=r"has no input type",
    ):
        service_handler(MyServiceHandlerWithOperationsWithoutTypeAnnotations)


async def test_logger_uses_operation_context(env: WorkflowEnvironment, caplog: Any):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    service_name = MyService.__name__
    operation_name = "log"
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=service_name,
    )
    caplog.set_level(logging.INFO)

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[MyServiceHandler()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        response = await service_client.start_operation(
            operation_name,
            dataclass_as_dict(Input("test_log")),
            {
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


@dataclass
class _InstantiationCase:
    executor: bool
    handler: Callable[..., Any]
    exception: type[Exception] | None
    match: str | None


@nexusrpc.service
class EchoService:
    echo: nexusrpc.Operation[Input, Output]


@service_handler(service=EchoService)
class SyncStartHandler:
    @sync_operation
    def echo(self, ctx: StartOperationContext, input: Input) -> Output:
        assert ctx.headers["test-header-key"] == "test-header-value"
        ctx.outbound_links.extend(ctx.inbound_links)
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )


@service_handler(service=EchoService)
class DefaultCancelHandler:
    @sync_operation
    async def echo(self, _ctx: StartOperationContext, input: Input) -> Output:
        return Output(
            value=f"from start method on {self.__class__.__name__}: {input.value}"
        )


@service_handler(service=EchoService)
class SyncCancelHandler:
    class SyncCancel(OperationHandler[Input, Output]):
        async def start(
            self,
            ctx: StartOperationContext,
            input: Input,
            # This return type is a type error, but VSCode doesn't flag it unless
            # "python.analysis.typeCheckingMode" is set to "strict"
        ) -> StartOperationResultSync[Output]:
            # Invalid: start method must wrap result as StartOperationResultSync
            # or StartOperationResultAsync
            return StartOperationResultSync(Output(value="Hello"))  # type: ignore

        def cancel(self, ctx: CancelOperationContext, token: str) -> None:
            return None  # type: ignore

    @operation_handler
    def echo(self) -> OperationHandler[Input, Output]:
        return SyncCancelHandler.SyncCancel()


class SyncHandlerNoExecutor(_InstantiationCase):
    handler = SyncStartHandler
    executor = False
    exception = RuntimeError
    match = "you have not supplied an executor"


class DefaultCancel(_InstantiationCase):
    handler = DefaultCancelHandler
    executor = False
    exception = None


class SyncCancel(_InstantiationCase):
    handler = SyncCancelHandler
    executor = False
    exception = RuntimeError
    match = "you have not supplied an executor"


@pytest.mark.parametrize(
    "test_case",
    [SyncHandlerNoExecutor, DefaultCancel, SyncCancel],
)
async def test_handler_instantiation(
    test_case: type[_InstantiationCase], client: Client
):
    task_queue = str(uuid.uuid4())

    if test_case.exception is not None:
        with pytest.raises(test_case.exception, match=test_case.match):
            Worker(
                client,
                task_queue=task_queue,
                nexus_service_handlers=[test_case.handler()],
                nexus_task_executor=ThreadPoolExecutor()
                if test_case.executor
                else None,
            )
    else:
        Worker(
            client,
            task_queue=task_queue,
            nexus_service_handlers=[test_case.handler()],
            nexus_task_executor=ThreadPoolExecutor() if test_case.executor else None,
        )


async def test_cancel_operation_with_invalid_token(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    """Verify that canceling an operation with an invalid token fails correctly."""
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=MyService.__name__,
    )

    decorator = service_handler(service=MyService)
    user_service_handler = decorator(MyServiceHandler)()

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[user_service_handler],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        cancel_response = await service_client.cancel_operation(
            "workflow_run_operation_happy_path",
            token="this-is-not-a-valid-token",
        )
        assert cancel_response.status_code == 404
        failure = Failure(**cancel_response.json())
        assert "failed to decode operation token" in failure.message.lower()


async def test_request_id_is_received_by_sync_operation(
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=MyService.__name__,
    )

    decorator = service_handler(service=MyService)
    user_service_handler = decorator(MyServiceHandler)()

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[user_service_handler],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        request_id = str(uuid.uuid4())
        resp = await service_client.start_operation(
            "idempotency_check", None, {"Nexus-Request-Id": request_id}
        )
        assert resp.status_code == 200
        assert resp.json() == {"value": f"request_id: {request_id}"}


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: Input) -> Output:
        return Output(value=input.value)


@service_handler
class ServiceHandlerForRequestIdTest:
    @workflow_run_operation
    async def operation_backed_by_a_workflow(
        self, ctx: WorkflowRunOperationContext, input: Input
    ) -> nexus.WorkflowHandle[Output]:
        return await ctx.start_workflow(
            EchoWorkflow.run,
            input,
            id=input.value,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    @workflow_run_operation
    async def operation_that_executes_a_workflow_before_starting_the_backing_workflow(
        self, ctx: WorkflowRunOperationContext, input: Input
    ) -> nexus.WorkflowHandle[Output]:
        await nexus.client().start_workflow(
            EchoWorkflow.run,
            input,
            id=input.value,
            task_queue=nexus.info().task_queue,
        )
        # This should fail. It will not fail if the Nexus request ID was incorrectly
        # propagated to both StartWorkflow requests.
        return await ctx.start_workflow(
            EchoWorkflow.run,
            input,
            id=input.value,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )


async def test_request_id_becomes_start_workflow_request_id(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # We send two Nexus requests that would start a workflow with the same workflow ID,
    # using reuse_policy=REJECT_DUPLICATE. This would fail if they used different
    # request IDs. However, when we use the same request ID, it does not fail,
    # demonstrating that the Nexus Start Operation request ID has become the
    # StartWorkflow request ID.
    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=ServiceHandlerForRequestIdTest.__name__,
    )

    async def start_two_workflows_with_conflicting_workflow_ids(
        request_ids: tuple[tuple[str, int, str], tuple[str, int, str]],
    ):
        workflow_id = str(uuid.uuid4())
        for request_id, status_code, error_message in request_ids:
            resp = await service_client.start_operation(
                "operation_backed_by_a_workflow",
                dataclass_as_dict(Input(workflow_id)),
                {"Nexus-Request-Id": request_id},
            )
            assert resp.status_code == status_code, (
                f"expected status code {status_code} "
                f"but got {resp.status_code} for response content "
                f"{pprint.pformat(resp.content.decode())}"
            )
            if not error_message:
                assert status_code == 201
                op_info = resp.json()
                assert op_info["token"]
                assert op_info["state"] == "running"
            else:
                assert status_code >= 400
                failure = Failure(**resp.json())
                assert failure.message == error_message

    async def start_two_workflows_in_a_single_operation(
        request_id: str, status_code: int, error_message: str
    ):
        resp = await service_client.start_operation(
            "operation_that_executes_a_workflow_before_starting_the_backing_workflow",
            dataclass_as_dict(Input("test-workflow-id")),
            {"Nexus-Request-Id": request_id},
        )
        assert resp.status_code == status_code
        if error_message:
            failure = Failure(**resp.json())
            assert failure.message == error_message

    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[ServiceHandlerForRequestIdTest()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        request_id_1, request_id_2 = str(uuid.uuid4()), str(uuid.uuid4())
        # Reusing the same request ID does not fail
        await start_two_workflows_with_conflicting_workflow_ids(
            ((request_id_1, 201, ""), (request_id_1, 201, ""))
        )
        # Using a different request ID does fail
        # TODO(nexus-prerelease) I think that this should be a 409 per the spec. Go and
        # Java are not doing that.
        await start_two_workflows_with_conflicting_workflow_ids(
            (
                (request_id_1, 201, ""),
                (request_id_2, 500, "Workflow execution already started"),
            )
        )
        # Two workflows started in the same operation should fail, since the Nexus
        # request ID should be propagated to the backing workflow only.
        await start_two_workflows_in_a_single_operation(
            request_id_1, 500, "Workflow execution already started"
        )
