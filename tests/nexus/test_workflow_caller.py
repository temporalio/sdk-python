from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any
from urllib.request import urlopen

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    StartOperationResultSync,
    service_handler,
    sync_operation,
)
from nexusrpc.handler._decorators import operation_handler

import temporalio.api
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.history.v1
import temporalio.nexus._operation_handlers
from temporalio import nexus, workflow
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.converter import PayloadConverter
from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    NexusOperationError,
)
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.runtime import (
    BUFFERED_METRIC_KIND_COUNTER,
    MetricBuffer,
    PrometheusConfig,
    Runtime,
    TelemetryConfig,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteNexusOperationCancelInput,
    ExecuteNexusOperationStartInput,
    Interceptor,
    NexusOperationInboundInterceptor,
    StartNexusOperationInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers import find_free_port, new_worker
from tests.helpers.metrics import PromMetricMatcher
from tests.helpers.nexus import make_nexus_endpoint_name

# TODO(nexus-preview): test worker shutdown, wait_all_completed, drain etc

# -----------------------------------------------------------------------------
# Test definition
#


class CallerReference(IntEnum):
    IMPL_WITHOUT_INTERFACE = 0
    IMPL_WITH_INTERFACE = 1
    INTERFACE = 2


class OpDefinitionType(IntEnum):
    SHORTHAND = 0
    LONGHAND = 1


@dataclass
class SyncResponse:
    op_definition_type: OpDefinitionType
    use_async_def: bool
    exception_in_operation_start: bool


@dataclass
class AsyncResponse:
    operation_workflow_id: str
    block_forever_waiting_for_cancellation: bool
    op_definition_type: OpDefinitionType
    exception_in_operation_start: bool


# The order of the two types in this union is critical since the data converter matches
# eagerly, ignoring unknown fields, and so would identify an AsyncResponse as a
# SyncResponse if SyncResponse came first.
ResponseType = AsyncResponse | SyncResponse

# -----------------------------------------------------------------------------
# Service interface
#


@dataclass
class OpInput:
    response_type: ResponseType
    headers: dict[str, str]
    caller_reference: CallerReference


@dataclass
class OpOutput:
    value: str


@dataclass
class HeaderTestOutput:
    received_headers: dict[str, str]


@dataclass
class HeaderTestCallerWfInput:
    headers: dict[str, str]
    task_queue: str


@dataclass
class CancelHeaderTestCallerWfInput:
    workflow_id: str
    headers: dict[str, str]
    task_queue: str


@dataclass
class WorkflowRunHeaderTestCallerWfInput:
    headers: dict[str, str]
    task_queue: str


@dataclass
class HandlerWfInput:
    op_input: OpInput


@dataclass
class HandlerWfOutput:
    value: str


@nexusrpc.service
class ServiceInterface:
    sync_or_async_operation: nexusrpc.Operation[OpInput, OpOutput]
    sync_operation: nexusrpc.Operation[OpInput, OpOutput]
    async_operation: nexusrpc.Operation[OpInput, HandlerWfOutput]


@nexusrpc.service
class HeaderTestService:
    header_echo_operation: nexusrpc.Operation[None, HeaderTestOutput]
    workflow_run_header_operation: nexusrpc.Operation[None, HeaderTestOutput]
    cancellable_operation: nexusrpc.Operation[None, str]


# -----------------------------------------------------------------------------
# Service implementation
#


@workflow.defn
class HandlerWorkflow:
    @workflow.run
    async def run(
        self,
        input: HandlerWfInput,
    ) -> HandlerWfOutput:
        assert isinstance(input.op_input.response_type, AsyncResponse)
        if input.op_input.response_type.block_forever_waiting_for_cancellation:
            await asyncio.Future()
        return HandlerWfOutput(
            value="workflow result",
        )


# TODO(nexus-prerelease): check type-checking passing in CI


class SyncOrAsyncOperation(OperationHandler[OpInput, OpOutput]):
    async def start(  # type: ignore[override]
        self, ctx: StartOperationContext, input: OpInput
    ) -> StartOperationResultSync[OpOutput] | StartOperationResultAsync:
        if input.response_type.exception_in_operation_start:
            raise RPCError(
                "RPCError INVALID_ARGUMENT in Nexus operation",
                RPCStatusCode.INVALID_ARGUMENT,
                b"",
            )
        if isinstance(input.response_type, SyncResponse):
            return StartOperationResultSync(value=OpOutput(value="sync response"))
        elif isinstance(input.response_type, AsyncResponse):
            # TODO(nexus-preview): what do we want the DX to be for a user who is
            # starting a Nexus backing workflow from a custom start method? (They may
            # need to do this in order to customize the cancel method).
            tctx = WorkflowRunOperationContext._from_start_operation_context(ctx)
            handle = await tctx.start_workflow(
                HandlerWorkflow.run,
                HandlerWfInput(op_input=input),
                id=input.response_type.operation_workflow_id,
            )
            return StartOperationResultAsync(handle.to_token())
        else:
            raise TypeError

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        return await temporalio.nexus._operation_handlers._cancel_workflow(token)


@service_handler(service=ServiceInterface)
class ServiceImpl:
    @operation_handler
    def sync_or_async_operation(
        self,
    ) -> OperationHandler[OpInput, OpOutput]:
        return SyncOrAsyncOperation()

    @sync_operation
    async def sync_operation(
        self, _ctx: StartOperationContext, input: OpInput
    ) -> OpOutput:
        assert isinstance(input.response_type, SyncResponse)
        if input.response_type.exception_in_operation_start:
            raise RPCError(
                "RPCError INVALID_ARGUMENT in Nexus operation",
                RPCStatusCode.INVALID_ARGUMENT,
                b"",
            )
        return OpOutput(value="sync response")

    @workflow_run_operation
    async def async_operation(
        self, ctx: WorkflowRunOperationContext, input: OpInput
    ) -> nexus.WorkflowHandle[HandlerWfOutput]:
        assert isinstance(input.response_type, AsyncResponse)
        if input.response_type.exception_in_operation_start:
            raise RPCError(
                "RPCError INVALID_ARGUMENT in Nexus operation",
                RPCStatusCode.INVALID_ARGUMENT,
                b"",
            )
        return await ctx.start_workflow(
            HandlerWorkflow.run,
            HandlerWfInput(op_input=input),
            id=input.response_type.operation_workflow_id,
        )


@workflow.defn
class HeaderEchoWorkflow:
    """A workflow that returns the headers it receives as input."""

    @workflow.run
    async def run(self, headers: dict[str, str]) -> HeaderTestOutput:
        return HeaderTestOutput(received_headers=headers)


class CancellableOperationHandler(OperationHandler[None, str]):
    """Operation handler that captures cancel headers."""

    def __init__(self, cancel_headers_received: list[dict[str, str]]) -> None:
        self._cancel_headers_received = cancel_headers_received

    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync("test-token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        # Capture cancel headers for test verification
        self._cancel_headers_received.append(
            {
                k: v
                for k, v in ctx.headers.items()
                if k.startswith("x-custom-") or k.startswith("x-interceptor-")
            }
        )


@service_handler(service=HeaderTestService)
class HeaderTestServiceImpl:
    def __init__(self) -> None:
        self.cancel_headers_received: list[dict[str, str]] = []

    @sync_operation
    async def header_echo_operation(
        self, ctx: StartOperationContext, _input: None
    ) -> HeaderTestOutput:
        # Return headers with "x-custom-" or "x-interceptor-" prefix for verification
        return HeaderTestOutput(
            received_headers={
                k: v
                for k, v in ctx.headers.items()
                if k.startswith("x-custom-") or k.startswith("x-interceptor-")
            }
        )

    @workflow_run_operation
    async def workflow_run_header_operation(
        self, ctx: WorkflowRunOperationContext, _input: None
    ) -> nexus.WorkflowHandle[HeaderTestOutput]:
        # Filter headers and pass to backing workflow
        filtered_headers = {
            k: v
            for k, v in ctx.headers.items()
            if k.startswith("x-custom-") or k.startswith("x-interceptor-")
        }
        return await ctx.start_workflow(
            HeaderEchoWorkflow.run,
            filtered_headers,
            id=str(uuid.uuid4()),
        )

    @operation_handler
    def cancellable_operation(self) -> OperationHandler[None, str]:
        return CancellableOperationHandler(self.cancel_headers_received)


# -----------------------------------------------------------------------------
# Caller workflow
#


@dataclass
class CallerWfInput:
    op_input: OpInput


@dataclass
class CallerWfOutput:
    op_output: OpOutput


@workflow.defn
class CallerWorkflow:
    """
    A workflow that executes a Nexus operation, specifying whether it should return
    synchronously or asynchronously.
    """

    @workflow.init
    def __init__(
        self,
        input: CallerWfInput,
        _request_cancel: bool,
        task_queue: str,
    ) -> None:
        self.nexus_client: workflow.NexusClient[ServiceInterface | ServiceImpl] = (  # type:ignore[reportAttributeAccessIssue]
            workflow.create_nexus_client(
                service={
                    CallerReference.IMPL_WITH_INTERFACE: ServiceImpl,
                    CallerReference.INTERFACE: ServiceInterface,
                }[input.op_input.caller_reference],
                endpoint=make_nexus_endpoint_name(task_queue),
            )
        )
        self._nexus_operation_start_resolved = False
        self._proceed = False

    @workflow.run
    async def run(
        self,
        input: CallerWfInput,
        request_cancel: bool,
        _task_queue: str,
    ) -> CallerWfOutput:
        op_input = input.op_input
        try:
            op_handle = await self.nexus_client.start_operation(
                self._get_operation(op_input),  # type: ignore[arg-type] # test uses non-public operation types
                op_input,
                headers=op_input.headers,
            )
        finally:
            self._nexus_operation_start_resolved = True
        if not input.op_input.response_type.exception_in_operation_start:
            if isinstance(input.op_input.response_type, SyncResponse):
                assert (
                    op_handle.operation_token is None
                ), "operation_token should be absent after a sync response"
            else:
                assert (
                    op_handle.operation_token
                ), "operation_token should be present after an async response"

        if request_cancel:
            # Even for SyncResponse, the op_handle future is not done at this point; that
            # transition doesn't happen until the handle is awaited.
            assert op_handle.cancel()
        op_output = await op_handle
        return CallerWfOutput(op_output=OpOutput(value=op_output.value))

    @workflow.update
    async def wait_nexus_operation_start_resolved(self) -> None:
        await workflow.wait_condition(lambda: self._nexus_operation_start_resolved)

    @staticmethod
    def _get_operation(
        op_input: OpInput,
    ) -> (
        nexusrpc.Operation[OpInput, OpOutput] | Callable[..., Awaitable[OpOutput]]
        # We are not exposing operation factory methods to users as a way to write nexus
        # operations, and accordingly the types on NexusClient
        # start_operation/execute_operation to not permit it. We fake the type by
        # pretending that this function doesn't return such operations.
        # Callable[[Any], OperationHandler[OpInput, OpOutput]],
    ):
        return {  # type: ignore[return-value]
            (
                SyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_operation,
            (
                SyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_operation,
            (
                SyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_or_async_operation,
            (
                SyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_or_async_operation,
            (
                AsyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.async_operation,
            (
                AsyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.async_operation,
            (
                AsyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_or_async_operation,
            (
                AsyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_or_async_operation,
        }[
            {True: SyncResponse, False: AsyncResponse}[
                isinstance(op_input.response_type, SyncResponse)
            ],
            op_input.response_type.op_definition_type,
            op_input.caller_reference,
            (
                op_input.response_type.use_async_def
                if isinstance(op_input.response_type, SyncResponse)
                else True
            ),
        ]


@workflow.defn
class UntypedCallerWorkflow:
    @workflow.init
    def __init__(
        self, input: CallerWfInput, request_cancel: bool, task_queue: str
    ) -> None:
        # TODO(nexus-preview): untyped caller cannot reference name of implementation. I think this is as it should be.
        service_name = "ServiceInterface"
        self.nexus_client: workflow.NexusClient[Any] = workflow.create_nexus_client(
            service=service_name,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

    @workflow.run
    async def run(
        self, input: CallerWfInput, _request_cancel: bool, _task_queue: str
    ) -> CallerWfOutput:
        op_input = input.op_input
        if op_input.response_type.op_definition_type == OpDefinitionType.LONGHAND:
            op_name = "sync_or_async_operation"
        elif isinstance(op_input.response_type, AsyncResponse):
            op_name = "async_operation"
        elif isinstance(op_input.response_type, SyncResponse):
            op_name = "sync_operation"
        else:
            raise TypeError

        arbitrary_condition = isinstance(op_input.response_type, SyncResponse)

        if arbitrary_condition:
            op_handle = await self.nexus_client.start_operation(
                op_name,
                op_input,
                headers=op_input.headers,
                output_type=OpOutput,
            )
            op_output = await op_handle
        else:
            op_output = await self.nexus_client.execute_operation(
                op_name,
                op_input,
                headers=op_input.headers,
                output_type=OpOutput,
            )
        return CallerWfOutput(op_output=OpOutput(value=op_output.value))


@workflow.defn
class HeaderTestCallerWorkflow:
    @workflow.run
    async def run(self, input: HeaderTestCallerWfInput) -> HeaderTestOutput:
        nexus_client = workflow.create_nexus_client(
            service=HeaderTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        return await nexus_client.execute_operation(
            HeaderTestService.header_echo_operation,
            None,
            headers=input.headers,
        )


@workflow.defn
class CancelHeaderTestCallerWorkflow:
    """Workflow that starts a cancellable operation and then cancels it."""

    @workflow.run
    async def run(self, input: CancelHeaderTestCallerWfInput) -> None:
        nexus_client = workflow.create_nexus_client(
            service=HeaderTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        op_handle = await nexus_client.start_operation(
            HeaderTestService.cancellable_operation,
            None,
            headers=input.headers,
        )
        # Request cancellation - this sends cancel headers to the handler
        op_handle.cancel()
        # Wait briefly to allow cancel request to be processed
        await asyncio.sleep(0.1)


@workflow.defn
class WorkflowRunHeaderTestCallerWorkflow:
    """Workflow that calls a workflow_run_operation and verifies headers."""

    @workflow.run
    async def run(self, input: WorkflowRunHeaderTestCallerWfInput) -> HeaderTestOutput:
        nexus_client = workflow.create_nexus_client(
            service=HeaderTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        return await nexus_client.execute_operation(
            HeaderTestService.workflow_run_header_operation,
            None,
            headers=input.headers,
        )


# -----------------------------------------------------------------------------
# Tests
#


async def test_sync_operation_happy_path(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        wf_output = await client.execute_workflow(
            CallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=SyncResponse(
                            op_definition_type=OpDefinitionType.SHORTHAND,
                            use_async_def=True,
                            exception_in_operation_start=False,
                        ),
                        headers={},
                        caller_reference=CallerReference.IMPL_WITH_INTERFACE,
                    ),
                ),
                False,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert wf_output.op_output.value == "sync response"


async def test_workflow_run_operation_happy_path(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        wf_output = await client.execute_workflow(
            CallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=AsyncResponse(
                            operation_workflow_id=str(uuid.uuid4()),
                            block_forever_waiting_for_cancellation=False,
                            op_definition_type=OpDefinitionType.SHORTHAND,
                            exception_in_operation_start=False,
                        ),
                        headers={},
                        caller_reference=CallerReference.IMPL_WITH_INTERFACE,
                    ),
                ),
                False,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert wf_output.op_output.value == "workflow result"


# TODO(nexus-preview): cross-namespace tests
# TODO(nexus-preview): nexus endpoint pytest fixture?


# -----------------------------------------------------------------------------
# Header tests
#


@dataclass
class HeaderModificationRecord:
    original_headers: dict[str, str]
    modified_headers: dict[str, str]


@dataclass
class CancelHeaderRecord:
    original_headers: dict[str, str]
    modified_headers: dict[str, str]


class HeaderModifyingNexusInterceptor(Interceptor):
    def __init__(self) -> None:
        self.header_records: list[HeaderModificationRecord] = []
        self.cancel_header_records: list[CancelHeaderRecord] = []

    def intercept_nexus_operation(
        self, next: NexusOperationInboundInterceptor
    ) -> NexusOperationInboundInterceptor:
        return _HeaderModifyingNexusInboundInterceptor(next, self)


class _HeaderModifyingNexusInboundInterceptor(NexusOperationInboundInterceptor):
    def __init__(
        self,
        next: NexusOperationInboundInterceptor,
        root: HeaderModifyingNexusInterceptor,
    ):
        super().__init__(next)
        self._root = root

    async def execute_nexus_operation_start(
        self, input: ExecuteNexusOperationStartInput
    ) -> StartOperationResultSync[Any] | StartOperationResultAsync:
        import dataclasses

        original_headers = dict(input.ctx.headers)

        # Modify headers: prefix values and add new header
        modified_headers = {
            k: f"interceptor-modified-{v}" if k.startswith("x-custom-") else v
            for k, v in input.ctx.headers.items()
        }
        modified_headers["x-interceptor-added"] = "interceptor-value"

        self._root.header_records.append(
            HeaderModificationRecord(
                original_headers=original_headers,
                modified_headers=modified_headers,
            )
        )

        input.ctx = dataclasses.replace(input.ctx, headers=modified_headers)
        return await super().execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: ExecuteNexusOperationCancelInput
    ) -> None:
        import dataclasses

        original_headers = dict(input.ctx.headers)

        # Modify headers: prefix values and add new header
        modified_headers = {
            k: f"interceptor-modified-{v}" if k.startswith("x-custom-") else v
            for k, v in input.ctx.headers.items()
        }
        modified_headers["x-interceptor-added"] = "cancel-interceptor-value"

        self._root.cancel_header_records.append(
            CancelHeaderRecord(
                original_headers=original_headers,
                modified_headers=modified_headers,
            )
        )

        input.ctx = dataclasses.replace(input.ctx, headers=modified_headers)
        return await super().execute_nexus_operation_cancel(input)


class HeaderAddingOutboundInterceptor(Interceptor):
    """Outbound interceptor that adds a static header to Nexus operation requests."""

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return _HeaderAddingWorkflowInboundInterceptor


class _HeaderAddingWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_HeaderAddingWorkflowOutboundInterceptor(outbound))


class _HeaderAddingWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    async def start_nexus_operation(
        self, input: StartNexusOperationInput
    ) -> workflow.NexusOperationHandle:
        existing_headers = dict(input.headers) if input.headers else {}
        existing_headers["x-custom-outbound"] = "outbound-value"
        input = dataclasses.replace(input, headers=existing_headers)
        return await super().start_nexus_operation(input)


async def test_start_operation_headers(
    client: Client,
    env: WorkflowEnvironment,
):
    """Test headers from workflow and interceptors are propagated to start operation handler."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    inbound_interceptor = HeaderModifyingNexusInterceptor()

    async with Worker(
        client,
        nexus_service_handlers=[HeaderTestServiceImpl()],
        workflows=[HeaderTestCallerWorkflow],
        task_queue=task_queue,
        interceptors=[HeaderAddingOutboundInterceptor(), inbound_interceptor],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        workflow_headers = {"x-custom-from-workflow": "workflow-value"}
        result = await client.execute_workflow(
            HeaderTestCallerWorkflow.run,
            HeaderTestCallerWfInput(headers=workflow_headers, task_queue=task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # Verify inbound interceptor saw headers from workflow and outbound interceptor
        assert len(inbound_interceptor.header_records) == 1
        record = inbound_interceptor.header_records[0]
        assert record.original_headers.get("x-custom-from-workflow") == "workflow-value"
        assert record.original_headers.get("x-custom-outbound") == "outbound-value"

        # Verify handler received headers modified by inbound interceptor
        assert (
            result.received_headers.get("x-custom-from-workflow")
            == "interceptor-modified-workflow-value"
        )
        assert (
            result.received_headers.get("x-custom-outbound")
            == "interceptor-modified-outbound-value"
        )
        assert result.received_headers.get("x-interceptor-added") == "interceptor-value"


async def test_workflow_run_operation_headers(
    client: Client,
    env: WorkflowEnvironment,
):
    """Test that headers are propagated to @workflow_run_operation handlers."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    test_headers = {"x-custom-workflow-run": "workflow-run-value"}

    async with Worker(
        client,
        nexus_service_handlers=[HeaderTestServiceImpl()],
        workflows=[WorkflowRunHeaderTestCallerWorkflow, HeaderEchoWorkflow],
        task_queue=task_queue,
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        result = await client.execute_workflow(
            WorkflowRunHeaderTestCallerWorkflow.run,
            WorkflowRunHeaderTestCallerWfInput(
                headers=test_headers, task_queue=task_queue
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert (
            result.received_headers.get("x-custom-workflow-run") == "workflow-run-value"
        )


async def test_cancel_operation_headers(
    client: Client,
    env: WorkflowEnvironment,
):
    """Test headers from workflow and interceptor are propagated to cancel operation handler."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    inbound_interceptor = HeaderModifyingNexusInterceptor()
    service_handler = HeaderTestServiceImpl()

    async with Worker(
        client,
        nexus_service_handlers=[service_handler],
        workflows=[CancelHeaderTestCallerWorkflow],
        task_queue=task_queue,
        interceptors=[inbound_interceptor],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        workflow_headers = {"x-custom-cancel": "cancel-value"}
        await client.execute_workflow(
            CancelHeaderTestCallerWorkflow.run,
            CancelHeaderTestCallerWfInput(
                workflow_id=workflow_id,
                headers=workflow_headers,
                task_queue=task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # Verify inbound interceptor saw cancel headers from workflow
        assert len(inbound_interceptor.cancel_header_records) == 1
        record = inbound_interceptor.cancel_header_records[0]
        assert record.original_headers.get("x-custom-cancel") == "cancel-value"

        # Verify handler received headers modified by inbound interceptor
        assert len(service_handler.cancel_headers_received) == 1
        received = service_handler.cancel_headers_received[0]
        assert received.get("x-custom-cancel") == "interceptor-modified-cancel-value"
        assert received.get("x-interceptor-added") == "cancel-interceptor-value"


@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize("request_cancel", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
async def test_sync_response(
    client: Client,
    env: WorkflowEnvironment,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        caller_wf_handle = await client.start_workflow(
            CallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=SyncResponse(
                            op_definition_type=op_definition_type,
                            use_async_def=True,
                            exception_in_operation_start=exception_in_operation_start,
                        ),
                        headers={"header-key": "header-value"},
                        caller_reference=caller_reference,
                    ),
                ),
                request_cancel,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # The operation result is returned even when request_cancel=True, because the
        # response was synchronous and it could not be cancelled. See explanation below.
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, nexusrpc.HandlerError)
            # ID of first command
            assert e.__cause__.scheduled_event_id == 5
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "sync_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
        else:
            result = await caller_wf_handle.result()
            assert result.op_output.value == "sync response"


@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize("request_cancel", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
async def test_async_response(
    client: Client,
    env: WorkflowEnvironment,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        caller_wf_handle, handler_wf_handle = await _start_wf_and_nexus_op(
            env,
            client,
            task_queue,
            exception_in_operation_start,
            request_cancel,
            op_definition_type,
            caller_reference,
        )
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, nexusrpc.HandlerError)
            # ID of first command after update accepted
            assert e.__cause__.scheduled_event_id == 6
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "async_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
            return

        handler_wf_info = await handler_wf_handle.describe()
        assert handler_wf_info.status in [
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
        ]
        await assert_handler_workflow_has_link_to_caller_workflow(
            caller_wf_handle, handler_wf_handle
        )
        await assert_caller_workflow_has_link_to_handler_workflow(
            caller_wf_handle, handler_wf_handle, handler_wf_info.run_id
        )

        if request_cancel:
            # The operation response was asynchronous and so request_cancel is honored. See
            # explanation below.
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, CancelledError)
            # ID of first command after update accepted
            assert e.__cause__.scheduled_event_id == 6
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "async_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
            assert nexus.WorkflowHandle.from_token(
                e.__cause__.operation_token
            ) == nexus.WorkflowHandle(
                namespace=handler_wf_handle._client.namespace,
                workflow_id=handler_wf_handle.id,
            )
            # Check that the handler workflow was canceled
            handler_wf_info = await handler_wf_handle.describe()
            assert handler_wf_info.status == WorkflowExecutionStatus.CANCELED
        else:
            handler_wf_info = await handler_wf_handle.describe()
            assert handler_wf_info.status == WorkflowExecutionStatus.COMPLETED
            result = await caller_wf_handle.result()
            assert result.op_output.value == "workflow result"


async def _start_wf_and_nexus_op(
    env: WorkflowEnvironment,
    client: Client,
    task_queue: str,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
) -> tuple[
    WorkflowHandle[CallerWorkflow, CallerWfOutput],
    WorkflowHandle[HandlerWorkflow, HandlerWfOutput],
]:
    """
    Start the caller workflow and wait until the Nexus operation has started.
    """
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    operation_workflow_id = str(uuid.uuid4())

    # Start the caller workflow and wait until it confirms the Nexus operation has started.
    block_forever_waiting_for_cancellation = request_cancel
    start_op = WithStartWorkflowOperation(
        CallerWorkflow.run,
        args=[
            CallerWfInput(
                op_input=OpInput(
                    response_type=AsyncResponse(
                        operation_workflow_id,
                        block_forever_waiting_for_cancellation,
                        op_definition_type,
                        exception_in_operation_start=exception_in_operation_start,
                    ),
                    headers={"header-key": "header-value"},
                    caller_reference=caller_reference,
                ),
            ),
            request_cancel,
            task_queue,
        ],
        id=str(uuid.uuid4()),
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )

    await client.execute_update_with_start_workflow(
        CallerWorkflow.wait_nexus_operation_start_resolved,
        start_workflow_operation=start_op,
    )
    caller_wf_handle = await start_op.workflow_handle()

    # check that the operation-backing workflow now exists, and that (a) the handler
    # workflow accepted the link to the calling Nexus event, and that (b) the caller
    # workflow NexusOperationStarted event received in return a link to the
    # operation-backing workflow.
    handler_wf_handle: WorkflowHandle[HandlerWorkflow, HandlerWfOutput] = (
        client.get_workflow_handle(operation_workflow_id)
    )
    return caller_wf_handle, handler_wf_handle


@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
@pytest.mark.parametrize("response_type", [SyncResponse, AsyncResponse])
async def test_untyped_caller(
    client: Client,
    env: WorkflowEnvironment,
    exception_in_operation_start: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
    response_type: ResponseType,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        workflows=[UntypedCallerWorkflow, HandlerWorkflow],
        nexus_service_handlers=[ServiceImpl()],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        if type(response_type) == SyncResponse:
            response_type = SyncResponse(
                op_definition_type=op_definition_type,
                use_async_def=True,
                exception_in_operation_start=exception_in_operation_start,
            )
        else:
            response_type = AsyncResponse(
                operation_workflow_id=str(uuid.uuid4()),
                block_forever_waiting_for_cancellation=False,
                op_definition_type=op_definition_type,
                exception_in_operation_start=exception_in_operation_start,
            )
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        caller_wf_handle = await client.start_workflow(
            UntypedCallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=response_type,
                        headers={},
                        caller_reference=caller_reference,
                    ),
                ),
                False,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, nexusrpc.HandlerError)
        else:
            result = await caller_wf_handle.result()
            assert result.op_output.value == (
                "sync response"
                if isinstance(response_type, SyncResponse)
                else "workflow result"
            )


#
# Test routing of workflow calls
#


@dataclass
class ServiceClassNameOutput:
    name: str


@nexusrpc.service
class ServiceInterfaceWithoutNameOverride:
    op: nexusrpc.Operation[None, ServiceClassNameOutput]


@nexusrpc.service(name="service-interface-🌈")
class ServiceInterfaceWithNameOverride:
    op: nexusrpc.Operation[None, ServiceClassNameOutput]


@service_handler
class ServiceImplInterfaceWithNeitherInterfaceNorNameOverride:
    @sync_operation
    async def op(
        self, _ctx: StartOperationContext, _input: None
    ) -> ServiceClassNameOutput:
        return ServiceClassNameOutput(self.__class__.__name__)


@service_handler(service=ServiceInterfaceWithoutNameOverride)
class ServiceImplInterfaceWithoutNameOverride:
    @sync_operation
    async def op(
        self, _ctx: StartOperationContext, _input: None
    ) -> ServiceClassNameOutput:
        return ServiceClassNameOutput(self.__class__.__name__)


@service_handler(service=ServiceInterfaceWithNameOverride)
class ServiceImplInterfaceWithNameOverride:
    @sync_operation
    async def op(
        self, _ctx: StartOperationContext, _input: None
    ) -> ServiceClassNameOutput:
        return ServiceClassNameOutput(self.__class__.__name__)


@service_handler(name="service-impl-🌈")
class ServiceImplWithNameOverride:
    @sync_operation
    async def op(
        self, _ctx: StartOperationContext, _input: None
    ) -> ServiceClassNameOutput:
        return ServiceClassNameOutput(self.__class__.__name__)


class NameOverride(IntEnum):
    NO = 0
    YES = 1


@workflow.defn
class ServiceInterfaceAndImplCallerWorkflow:
    @workflow.run
    async def run(
        self,
        caller_reference: CallerReference,
        name_override: NameOverride,
        task_queue: str,
    ) -> ServiceClassNameOutput:
        C, N = CallerReference, NameOverride
        service_cls: type
        if (caller_reference, name_override) == (C.INTERFACE, N.YES):
            service_cls = ServiceInterfaceWithNameOverride
        elif (caller_reference, name_override) == (C.INTERFACE, N.NO):
            service_cls = ServiceInterfaceWithoutNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITH_INTERFACE, N.YES):
            service_cls = ServiceImplWithNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITH_INTERFACE, N.NO):
            service_cls = ServiceImplInterfaceWithoutNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITHOUT_INTERFACE, N.NO):
            service_cls = ServiceImplInterfaceWithNameOverride
            service_cls = ServiceImplInterfaceWithNeitherInterfaceNorNameOverride
        else:
            raise ValueError(
                f"Invalid combination of caller_reference ({caller_reference}) and name_override ({name_override})"
            )

        nexus_client: workflow.NexusClient[Any] = workflow.create_nexus_client(
            service=service_cls,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

        return await nexus_client.execute_operation(service_cls.op, None)  # type: ignore


async def test_service_interface_and_implementation_names(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Note that:
    # - The caller can specify the service & operation via a reference to either the
    #   interface or implementation class.
    # - An interface class may optionally override its name.
    # - An implementation class may either override its name or specify an interface that
    #   it is implementing, but not both.
    # - On registering a service implementation with a worker, the name by which the
    #   service is addressed in requests is the interface name if the implementation
    #   supplies one, or else the name override made by the impl class, or else the impl
    #   class name.
    #
    # This test checks that the request is routed to the expected service under a variety
    # of scenarios related to the above considerations.
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[
            ServiceImplWithNameOverride(),
            ServiceImplInterfaceWithNameOverride(),
            ServiceImplInterfaceWithoutNameOverride(),
            ServiceImplInterfaceWithNeitherInterfaceNorNameOverride(),
        ],
        workflows=[ServiceInterfaceAndImplCallerWorkflow],
        task_queue=task_queue,
        workflow_failure_exception_types=[Exception],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(CallerReference.INTERFACE, NameOverride.YES, task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(CallerReference.INTERFACE, NameOverride.NO, task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithoutNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITH_INTERFACE,
                NameOverride.YES,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplWithNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITH_INTERFACE,
                NameOverride.NO,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithoutNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITHOUT_INTERFACE,
                NameOverride.NO,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput(
            "ServiceImplInterfaceWithNeitherInterfaceNorNameOverride"
        )


@nexusrpc.service
class ServiceWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow:
    my_workflow_run_operation: nexusrpc.Operation[None, None]
    my_manual_async_operation: nexusrpc.Operation[None, None]


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


@service_handler
class ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow:
    @workflow_run_operation
    async def my_workflow_run_operation(
        self, ctx: WorkflowRunOperationContext, _input: None
    ) -> nexus.WorkflowHandle[str]:
        result_1 = await nexus.client().execute_workflow(
            EchoWorkflow.run,
            "result-1",
            id=str(uuid.uuid4()),
            task_queue=nexus.info().task_queue,
        )
        # In case result_1 is incorrectly being delivered to the caller as the operation
        # result, give time for that incorrect behavior to occur.
        await asyncio.sleep(0.5)
        return await ctx.start_workflow(
            EchoWorkflow.run,
            f"{result_1}-result-2",
            id=str(uuid.uuid4()),
        )


@workflow.defn
class WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow:
    @workflow.run
    async def run(self, _input: str, task_queue: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

        return await nexus_client.execute_operation(
            ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow.my_workflow_run_operation,
            None,
        )


async def test_workflow_run_operation_can_execute_workflow_before_starting_backing_workflow(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        workflows=[
            EchoWorkflow,
            WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow,
        ],
        nexus_service_handlers=[
            ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow(),
        ],
        task_queue=task_queue,
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        result = await client.execute_workflow(
            WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow.run,
            args=("result-1", task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == "result-1-result-2"


@service_handler
class SimpleSyncService:
    @sync_operation
    async def sync_op(self, _ctx: StartOperationContext, input: str) -> str:
        return input


@workflow.defn
class ExecuteNexusOperationWithSummaryWorkflow:
    @workflow.run
    async def run(self, input: str, task_queue: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=SimpleSyncService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

        op_result = await nexus_client.execute_operation(
            SimpleSyncService.sync_op, input, summary="nexus operation summary"
        )

        if op_result != input:
            raise ApplicationError("expected nexus operation to echo input")

        return op_result


async def test_nexus_operation_summary(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = f"task-queue-{uuid.uuid4()}"
    async with Worker(
        client,
        workflows=[ExecuteNexusOperationWithSummaryWorkflow],
        nexus_service_handlers=[
            SimpleSyncService(),
        ],
        task_queue=task_queue,
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        wf_id = f"wf-{uuid.uuid4()}"
        handle = await client.start_workflow(
            ExecuteNexusOperationWithSummaryWorkflow.run,
            args=("success", task_queue),
            id=wf_id,
            task_queue=task_queue,
        )
        result = await handle.result()
        assert result == "success"

        history = await handle.fetch_history()

        nexus_events = [
            event
            for event in history.events
            if event.HasField("nexus_operation_scheduled_event_attributes")
        ]
        assert len(nexus_events) == 1
        summary_value = PayloadConverter.default.from_payload(
            nexus_events[0].user_metadata.summary
        )
        assert summary_value == "nexus operation summary"


# TODO(nexus-prerelease): test invalid service interface implementations
# TODO(nexus-prerelease): test caller passing output_type


async def assert_caller_workflow_has_link_to_handler_workflow(
    caller_wf_handle: WorkflowHandle,
    handler_wf_handle: WorkflowHandle,
    handler_wf_run_id: str,
):
    caller_history = await caller_wf_handle.fetch_history()
    op_started_event = next(
        e
        for e in caller_history.events
        if (
            e.event_type
            == temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED
        )
    )
    if not len(op_started_event.links) == 1:
        pytest.fail(
            f"Expected 1 link on NexusOperationStarted event, got {len(op_started_event.links)}"
        )
    [link] = op_started_event.links
    assert link.workflow_event.namespace == handler_wf_handle._client.namespace
    assert link.workflow_event.workflow_id == handler_wf_handle.id
    assert link.workflow_event.run_id
    assert link.workflow_event.run_id == handler_wf_run_id
    assert (
        link.workflow_event.event_ref.event_type
        == temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
    )


async def assert_handler_workflow_has_link_to_caller_workflow(
    caller_wf_handle: WorkflowHandle,
    handler_wf_handle: WorkflowHandle,
):
    handler_history = await handler_wf_handle.fetch_history()
    wf_started_event = next(
        e
        for e in handler_history.events
        if (
            e.event_type
            == temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
        )
    )
    links = _get_links_from_workflow_execution_started_event(wf_started_event)
    if not len(links) == 1:
        pytest.fail(
            f"Expected 1 link on WorkflowExecutionStarted event, got {len(links)}"
        )
    [link] = links
    assert link.workflow_event.namespace == caller_wf_handle._client.namespace
    assert link.workflow_event.workflow_id == caller_wf_handle.id
    assert link.workflow_event.run_id
    assert link.workflow_event.run_id == caller_wf_handle.first_execution_run_id
    assert (
        link.workflow_event.event_ref.event_type
        == temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
    )


def _get_links_from_workflow_execution_started_event(
    event: temporalio.api.history.v1.HistoryEvent,
) -> list[temporalio.api.common.v1.Link]:
    [callback] = event.workflow_execution_started_event_attributes.completion_callbacks
    if links := callback.links:
        return list(links)
    else:
        return list(event.links)


# When request_cancel is True, the NexusOperationHandle in the workflow evolves
# through the following states:
#                           start_fut             result_fut            handle_task w/ fut_waiter                        (task._must_cancel)
#
# Case 1: Sync Nexus operation response w/ cancellation of NexusOperationHandle
# -----------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# before op_handle.cancel : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# Future_8240[FINISHED].cancel() -> False  # no state transition; fut_waiter is already finished
# cancel returned         : True
# before await op_handle  : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (True)
# --> Despite cancel having been requested, this await on the nexus op handle does not
#     raise CancelledError, because the task's underlying fut_waiter is already finished.
# after await op_handle   : Future_7856[FINISHED] Future_7984[FINISHED] Task[FINISHED] fut_waiter = None) (False)
#
#
# Case 2: Async Nexus operation response w/ cancellation of NexusOperationHandle
# ------------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# >>>>>>>>>>>> WFT 2
# >>>>>>>>>>>> WFT 3
# after await proceed     : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# before op_handle.cancel : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# Future_7952[PENDING].cancel() -> True  # transition to cancelled state; fut_waiter was not finished
# cancel returned         : True
# before await op_handle  : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[CANCELLED]) (False)
# --> This await on the nexus op handle raises CancelledError, because the task's underlying fut_waiter is cancelled.
#
# Thus in the sync case, although the caller workflow attempted to cancel the
# NexusOperationHandle, this did not result in a CancelledError when the handle was
# awaited, because both resolve_nexus_operation_start and resolve_nexus_operation jobs
# were sent in the same activation and hence the task's fut_waiter was already finished.
#
# But in the async case, at the time that we cancel the NexusOperationHandle, only the
# resolve_nexus_operation_start job had been sent; the result_fut was unresolved. Thus
# when the handle was awaited, CancelledError was raised.
#
# To create output like that above, set the following __repr__s:
# asyncio.Future:
# def __repr__(self):
#     return f"{self.__class__.__name__}_{str(id(self))[-4:]}[{self._state}]"
# _NexusOperationHandle:
# def __repr__(self) -> str:
#     return (
#         f"{self._start_fut} "
#         f"{self._result_fut} "
#         f"Task[{self._task._state}] fut_waiter = {self._task._fut_waiter}) ({self._task._must_cancel})"
#     )


# Test overloads


@dataclass
class OverloadTestValue:
    value: int


@workflow.defn
class OverloadTestHandlerWorkflow:
    @workflow.run
    async def run(self, input: OverloadTestValue) -> OverloadTestValue:
        return OverloadTestValue(value=input.value * 2)


@workflow.defn
class OverloadTestHandlerWorkflowNoParam:
    @workflow.run
    async def run(self) -> OverloadTestValue:
        return OverloadTestValue(value=0)


@nexusrpc.handler.service_handler
class OverloadTestServiceHandler:
    @workflow_run_operation
    async def no_param(
        self,
        ctx: WorkflowRunOperationContext,
        _: OverloadTestValue,
    ) -> nexus.WorkflowHandle[OverloadTestValue]:
        return await ctx.start_workflow(
            OverloadTestHandlerWorkflowNoParam.run,
            id=str(uuid.uuid4()),
        )

    @workflow_run_operation
    async def single_param(
        self, ctx: WorkflowRunOperationContext, input: OverloadTestValue
    ) -> nexus.WorkflowHandle[OverloadTestValue]:
        return await ctx.start_workflow(
            OverloadTestHandlerWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )

    @workflow_run_operation
    async def multi_param(
        self, ctx: WorkflowRunOperationContext, input: OverloadTestValue
    ) -> nexus.WorkflowHandle[OverloadTestValue]:
        return await ctx.start_workflow(
            OverloadTestHandlerWorkflow.run,
            args=[input],
            id=str(uuid.uuid4()),
        )

    @workflow_run_operation
    async def by_name(
        self, ctx: WorkflowRunOperationContext, input: OverloadTestValue
    ) -> nexus.WorkflowHandle[OverloadTestValue]:
        return await ctx.start_workflow(
            "OverloadTestHandlerWorkflow",
            input,
            id=str(uuid.uuid4()),
            result_type=OverloadTestValue,
        )

    @workflow_run_operation
    async def by_name_multi_param(
        self, ctx: WorkflowRunOperationContext, input: OverloadTestValue
    ) -> nexus.WorkflowHandle[OverloadTestValue]:
        return await ctx.start_workflow(
            "OverloadTestHandlerWorkflow",
            args=[input],
            id=str(uuid.uuid4()),
        )


@dataclass
class OverloadTestInput:
    op: Callable[
        [Any, WorkflowRunOperationContext, Any],
        Awaitable[temporalio.nexus.WorkflowHandle[Any]],
    ]
    input: Any
    output: Any


@workflow.defn
class OverloadTestCallerWorkflow:
    @workflow.run
    async def run(self, op: str, input: OverloadTestValue) -> OverloadTestValue:
        nexus_client = workflow.create_nexus_client(
            service=OverloadTestServiceHandler,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

        if op == "no_param":
            return await nexus_client.execute_operation(
                OverloadTestServiceHandler.no_param, input
            )
        elif op == "single_param":
            return await nexus_client.execute_operation(
                OverloadTestServiceHandler.single_param, input
            )
        elif op == "multi_param":
            return await nexus_client.execute_operation(
                OverloadTestServiceHandler.multi_param, input
            )
        elif op == "by_name":
            return await nexus_client.execute_operation(
                OverloadTestServiceHandler.by_name, input
            )
        elif op == "by_name_multi_param":
            return await nexus_client.execute_operation(
                OverloadTestServiceHandler.by_name_multi_param, input
            )
        else:
            raise ValueError(f"Unknown op: {op}")


@pytest.mark.parametrize(
    "op",
    [
        "no_param",
        "single_param",
        "multi_param",
        "by_name",
        "by_name_multi_param",
    ],
)
async def test_workflow_run_operation_overloads(
    client: Client, env: WorkflowEnvironment, op: str
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[
            OverloadTestCallerWorkflow,
            OverloadTestHandlerWorkflow,
            OverloadTestHandlerWorkflowNoParam,
        ],
        nexus_service_handlers=[OverloadTestServiceHandler()],
    ):
        endpoint_name = make_nexus_endpoint_name(task_queue)
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        res = await client.execute_workflow(
            OverloadTestCallerWorkflow.run,
            args=[op, OverloadTestValue(value=2)],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert res == (
            OverloadTestValue(value=4)
            if op != "no_param"
            else OverloadTestValue(value=0)
        )


@nexusrpc.handler.service_handler
class CustomMetricsService:
    @nexusrpc.handler.sync_operation
    async def custom_metric_op(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: None
    ) -> None:
        counter = nexus.metric_meter().create_counter(
            "my-operation-counter", "my-operation-description", "my-operation-unit"
        )
        counter.add(12)
        counter.add(30, {"my-operation-extra-attr": 12.34})

    @nexusrpc.handler.sync_operation
    def custom_metric_op_executor(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: None
    ) -> None:
        counter = nexus.metric_meter().create_counter(
            "my-executor-operation-counter",
            "my-executor-operation-description",
            "my-executor-operation-unit",
        )
        counter.add(12)
        counter.add(30, {"my-executor-operation-extra-attr": 12.34})


@workflow.defn
class CustomMetricsWorkflow:
    @workflow.run
    async def run(self, task_queue: str) -> None:
        nexus_client = workflow.create_nexus_client(
            service=CustomMetricsService, endpoint=make_nexus_endpoint_name(task_queue)
        )

        await nexus_client.execute_operation(
            CustomMetricsService.custom_metric_op, None
        )
        await nexus_client.execute_operation(
            CustomMetricsService.custom_metric_op_executor, None
        )


async def test_workflow_caller_custom_metrics(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)

    # Create new runtime with Prom server
    prom_addr = f"127.0.0.1:{find_free_port()}"
    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=prom_addr), metric_prefix="foo_"
        )
    )

    # New client with the runtime
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )

    async with new_worker(
        client,
        CustomMetricsWorkflow,
        task_queue=task_queue,
        nexus_service_handlers=[CustomMetricsService()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ) as worker:
        # Run workflow
        await client.execute_workflow(
            CustomMetricsWorkflow.run,
            worker.task_queue,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Get Prom dump
        with urlopen(url=f"http://{prom_addr}/metrics") as f:
            prom_str: str = f.read().decode("utf-8")
            prom_lines = prom_str.splitlines()

        prom_matcher = PromMetricMatcher(prom_lines)

        prom_matcher.assert_description_exists(
            "my_operation_counter", "my-operation-description"
        )
        prom_matcher.assert_metric_exists("my_operation_counter", {}, 12)
        prom_matcher.assert_metric_exists(
            "my_operation_counter",
            {
                "my_operation_extra_attr": "12.34",
                # Also confirm some nexus operation labels
                "nexus_service": CustomMetricsService.__name__,
                "nexus_operation": CustomMetricsService.custom_metric_op.__name__,
                "task_queue": worker.task_queue,
            },
            30,
        )
        prom_matcher.assert_description_exists(
            "my_executor_operation_counter", "my-executor-operation-description"
        )
        prom_matcher.assert_metric_exists("my_executor_operation_counter", {}, 12)
        prom_matcher.assert_metric_exists(
            "my_executor_operation_counter",
            {
                "my_executor_operation_extra_attr": "12.34",
                # Also confirm some nexus operation labels
                "nexus_service": CustomMetricsService.__name__,
                "nexus_operation": CustomMetricsService.custom_metric_op_executor.__name__,
                "task_queue": worker.task_queue,
            },
            30,
        )


async def test_workflow_caller_buffered_metrics(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Create runtime with metric buffer
    buffer = MetricBuffer(10000)
    runtime = Runtime(
        telemetry=TelemetryConfig(metrics=buffer, metric_prefix="some_prefix_")
    )

    # Confirm no updates yet
    assert not buffer.retrieve_updates()

    # Create a new client on the runtime and execute the custom metric workflow
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with new_worker(
        client,
        CustomMetricsWorkflow,
        task_queue=task_queue,
        nexus_service_handlers=[CustomMetricsService()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ) as worker:
        await client.execute_workflow(
            CustomMetricsWorkflow.run,
            worker.task_queue,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Drain updates and confirm updates exist as expected
    updates = buffer.retrieve_updates()
    # Check for Nexus metrics
    assert any(
        update.metric.name == "my-operation-counter"
        and update.metric.kind == BUFFERED_METRIC_KIND_COUNTER
        and update.metric.description == "my-operation-description"
        and update.attributes["nexus_service"] == CustomMetricsService.__name__
        and update.attributes["nexus_operation"]
        == CustomMetricsService.custom_metric_op.__name__
        and update.attributes["task_queue"] == worker.task_queue
        and "my-operation-extra-attr" not in update.attributes
        and update.value == 12
        for update in updates
    )
    assert any(
        update.metric.name == "my-operation-counter"
        and update.attributes.get("my-operation-extra-attr") == 12.34
        and update.value == 30
        for update in updates
    )
    assert any(
        update.metric.name == "my-executor-operation-counter"
        and update.metric.description == "my-executor-operation-description"
        and update.metric.kind == BUFFERED_METRIC_KIND_COUNTER
        and update.attributes["nexus_service"] == CustomMetricsService.__name__
        and update.attributes["nexus_operation"]
        == CustomMetricsService.custom_metric_op_executor.__name__
        and update.attributes["task_queue"] == worker.task_queue
        and "my-executor-operation-extra-attr" not in update.attributes
        and update.value == 12
        for update in updates
    )
    assert any(
        update.metric.name == "my-executor-operation-counter"
        and update.attributes.get("my-executor-operation-extra-attr") == 12.34
        and update.value == 30
        for update in updates
    )


@workflow.defn()
class CancelTestCallerWorkflow:
    def __init__(self) -> None:
        self.released = False

    @workflow.run
    async def run(self, use_async_cancel: bool, task_queue: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=TestAsyncAndNonAsyncCancel.CancelTestService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

        op = (
            TestAsyncAndNonAsyncCancel.CancelTestService.async_cancel_op
            if use_async_cancel
            else TestAsyncAndNonAsyncCancel.CancelTestService.non_async_cancel_op
        )

        # Start the operation and immediately request cancellation
        # Use WAIT_REQUESTED since we just need to verify the cancel handler was called
        handle = await nexus_client.start_operation(
            op,
            None,
            cancellation_type=workflow.NexusOperationCancellationType.WAIT_REQUESTED,
        )

        # Cancel the handle to trigger the cancel method on the handler
        handle.cancel()

        try:
            await handle
        except NexusOperationError:
            # Wait for release signal before completing
            await workflow.wait_condition(lambda: self.released)
            return "cancelled_successfully"

        return "unexpected_completion"

    @workflow.signal
    def release(self) -> None:
        self.released = True


@pytest.fixture(scope="class")
def cancel_test_events(request: pytest.FixtureRequest):
    if request.cls:
        request.cls.called_async = asyncio.Event()
        request.cls.called_non_async = threading.Event()
    yield


@pytest.mark.usefixtures("cancel_test_events")
class TestAsyncAndNonAsyncCancel:
    called_async: asyncio.Event  # pyright: ignore[reportUninitializedInstanceVariable]
    called_non_async: threading.Event  # pyright: ignore[reportUninitializedInstanceVariable]

    class OpWithAsyncCancel(OperationHandler[None, str]):
        def __init__(self, evt: asyncio.Event) -> None:
            self.evt = evt

        async def start(
            self, ctx: StartOperationContext, input: None
        ) -> StartOperationResultAsync:
            return StartOperationResultAsync("test-token")

        async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
            self.evt.set()

    class OpWithNonAsyncCancel(OperationHandler[None, str]):
        def __init__(self, evt: threading.Event) -> None:
            self.evt = evt

        def start(
            self, ctx: StartOperationContext, input: None
        ) -> StartOperationResultAsync:
            return StartOperationResultAsync("test-token")

        def cancel(self, ctx: CancelOperationContext, token: str) -> None:
            self.evt.set()

    @nexusrpc.service
    class CancelTestService:
        async_cancel_op: nexusrpc.Operation[None, str]
        non_async_cancel_op: nexusrpc.Operation[None, str]

    @service_handler(service=CancelTestService)
    class CancelTestServiceHandler:
        def __init__(
            self, async_evt: asyncio.Event, non_async_evt: threading.Event
        ) -> None:
            self.async_evt = async_evt
            self.non_async_evt = non_async_evt

        @operation_handler
        def async_cancel_op(self) -> OperationHandler[None, str]:
            return TestAsyncAndNonAsyncCancel.OpWithAsyncCancel(self.async_evt)

        @operation_handler
        def non_async_cancel_op(self) -> OperationHandler[None, str]:
            return TestAsyncAndNonAsyncCancel.OpWithNonAsyncCancel(self.non_async_evt)

    @pytest.mark.parametrize("use_async_cancel", [True, False])
    async def test_task_executor_operation_cancel_method(
        self, client: Client, env: WorkflowEnvironment, use_async_cancel: bool
    ):
        """Test that both async and non-async cancel methods work for TaskExecutor-based operations."""
        if env.supports_time_skipping:
            pytest.skip("Nexus tests don't work with time-skipping server")

        task_queue = str(uuid.uuid4())
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[CancelTestCallerWorkflow],
            nexus_service_handlers=[
                TestAsyncAndNonAsyncCancel.CancelTestServiceHandler(
                    self.called_async, self.called_non_async
                )
            ],
            nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
        ):
            endpoint_name = make_nexus_endpoint_name(task_queue)
            await env.create_nexus_endpoint(endpoint_name, task_queue)

            caller_wf_handle = await client.start_workflow(
                CancelTestCallerWorkflow.run,
                args=[use_async_cancel, task_queue],
                id=f"caller-wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

            # Wait for the cancel method to be called
            fut = (
                self.called_async.wait()
                if use_async_cancel
                else asyncio.get_running_loop().run_in_executor(
                    None, self.called_non_async.wait
                )
            )
            await asyncio.wait_for(fut, timeout=30)

            # Release the workflow to complete
            await caller_wf_handle.signal(CancelTestCallerWorkflow.release)

            # Verify the workflow completed successfully
            result = await caller_wf_handle.result()
            assert result == "cancelled_successfully"
