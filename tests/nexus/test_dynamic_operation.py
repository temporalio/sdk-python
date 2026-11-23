import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest import mock

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler._core import ServiceHandler

import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.nexus
import temporalio.worker._nexus
from temporalio import activity, workflow
from temporalio.client import (
    ActivityExecutionStatus,
    Client,
    NexusClient,
    NexusOperationFailureError,
    NexusOperationHandle,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowUpdateStage,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteNexusOperationCancelInput,
    ExecuteNexusOperationStartInput,
    Interceptor,
    NexusOperationInboundInterceptor,
    Worker,
)
from tests.helpers import assert_eventually
from tests.helpers.nexus import make_nexus_endpoint_name


@workflow.defn
class WaitingWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, value: str) -> str:
        return value


@workflow.defn
class UpdateTargetWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.update
    async def block(self) -> None:
        await workflow.wait_condition(lambda: False)


# The decorator converts Nexus input to RawValue and preserves results and handler errors.
@pytest.mark.parametrize("name", ["get_weather", "async", "reject"])
async def test_dynamic_temporal_decorator(name: str):
    @nexusrpc.handler.service_handler(name="tools")
    class CatchAllService:
        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            ctx: temporalio.nexus.TemporalStartOperationContext,
            _client: temporalio.nexus.TemporalNexusClient,
            input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            arguments = temporalio.converter.PayloadConverter.default.from_payload(
                input.payload, dict
            )
            if ctx.operation == "async":
                return temporalio.nexus.TemporalOperationResult.async_token(
                    "operation-token"
                )
            if ctx.operation == "reject":
                raise nexusrpc.HandlerError(
                    "Unknown tool", type=nexusrpc.HandlerErrorType.NOT_FOUND
                )
            return temporalio.nexus.TemporalOperationResult.sync(
                (ctx.operation, ctx.request_id, arguments)
            )

    service = ServiceHandler.from_user_instance(CatchAllService())
    assert not service.operation_handlers
    handler = service.get_operation_handler(name)
    assert isinstance(handler, temporalio.nexus.TemporalOperationHandler)
    assert handler is service.dynamic_handler
    ctx = temporalio.nexus.TemporalStartOperationContext(
        service="tools",
        operation=name,
        headers={},
        request_id="request-id",
        task_cancellation=temporalio.worker._nexus._NexusTaskCancellation(),
    )
    serializer = mock.Mock()
    payload = temporalio.converter.PayloadConverter.default.to_payload(
        {"city": "Boston"}
    )
    serializer.deserialize = mock.AsyncMock(
        return_value=temporalio.common.RawValue(payload)
    )
    raw = nexusrpc.LazyValue(serializer=serializer, headers={})
    if name == "reject":
        with pytest.raises(nexusrpc.HandlerError) as error:
            await handler.start_operation(ctx, mock.Mock(), raw)
        assert error.value.type == nexusrpc.HandlerErrorType.NOT_FOUND
    else:
        result = await handler.start_operation(ctx, mock.Mock(), raw)
        if name == "async":
            nexus_result = result._to_nexus_result()
            assert isinstance(nexus_result, nexusrpc.handler.StartOperationResultAsync)
            assert nexus_result.token == "operation-token"
        else:
            assert result.value == (name, "request-id", {"city": "Boston"})
    assert (
        serializer.deserialize.call_args.kwargs["as_type"] is temporalio.common.RawValue
    )


# A dynamic Temporal operation cannot specify a name, including an empty string.
@pytest.mark.parametrize("name", ["named", ""])
def test_dynamic_temporal_decorator_rejects_name(name: str):
    with pytest.raises(ValueError, match="cannot specify name"):
        temporalio.nexus.temporal_operation(name=name, dynamic=True)


# Dynamic Temporal operations reject decoded input annotations.
def test_dynamic_temporal_decorator_requires_raw_input():
    with pytest.raises(TypeError, match="RawValue"):

        class _Decoded:
            @temporalio.nexus.temporal_operation(dynamic=True)
            async def forward(
                self,
                _ctx: temporalio.nexus.TemporalStartOperationContext,
                _client: temporalio.nexus.TemporalNexusClient,
                input: dict,
            ) -> temporalio.nexus.TemporalOperationResult[Any]:
                return temporalio.nexus.TemporalOperationResult.sync(input)

        _ = _Decoded


# A service rejects a second fallback, including one inherited from a parent class.
def test_second_dynamic_temporal_operation_fails_registration():
    @nexusrpc.handler.service_handler(name="tools")
    class Parent:
        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            _ctx: temporalio.nexus.TemporalStartOperationContext,
            _client: temporalio.nexus.TemporalNexusClient,
            _input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            raise NotImplementedError

    with pytest.raises(RuntimeError, match="more than one dynamic"):

        @nexusrpc.handler.service_handler(name="duplicate")
        class _Duplicate(Parent):
            @temporalio.nexus.temporal_operation(dynamic=True)
            async def other(
                self,
                _ctx: temporalio.nexus.TemporalStartOperationContext,
                _client: temporalio.nexus.TemporalNexusClient,
                _input: temporalio.common.RawValue,
            ) -> temporalio.nexus.TemporalOperationResult[Any]:
                raise NotImplementedError

        _ = _Duplicate


@dataclass
class CallerInput:
    endpoint: str
    operation: str
    value: str


@workflow.defn
class DynamicCallerWorkflow:
    @workflow.run
    async def run(self, input: CallerInput) -> Any:
        client = workflow.create_nexus_client(
            service="dynamic-tools", endpoint=input.endpoint
        )
        return await client.execute_operation(input.operation, input.value)


@workflow.defn
class DynamicCancelCallerWorkflow:
    def __init__(self) -> None:
        self.started = False

    @workflow.run
    async def run(self, input: CallerInput) -> None:
        client = workflow.create_nexus_client(
            service="dynamic-tools", endpoint=input.endpoint
        )
        handle: workflow.NexusOperationHandle[Any] = await client.start_operation(
            input.operation, input.value
        )
        self.started = True
        await handle

    @workflow.update
    async def wait_started(self) -> None:
        await workflow.wait_condition(lambda: self.started)


class RecordingInterceptor(Interceptor):
    def __init__(self) -> None:
        self.starts: list[tuple[str, type]] = []
        self.cancels: list[str] = []

    def intercept_nexus_operation(
        self, next: NexusOperationInboundInterceptor
    ) -> NexusOperationInboundInterceptor:
        return _RecordingInbound(next, self)


class _RecordingInbound(NexusOperationInboundInterceptor):
    def __init__(
        self, next: NexusOperationInboundInterceptor, root: RecordingInterceptor
    ) -> None:
        super().__init__(next)
        self._root = root

    async def execute_nexus_operation_start(
        self, input: ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any]
        | nexusrpc.handler.StartOperationResultAsync
    ):
        self._root.starts.append((input.ctx.operation, type(input.input)))
        return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: ExecuteNexusOperationCancelInput
    ) -> None:
        self._root.cancels.append(input.ctx.operation)
        return await self.next.execute_nexus_operation_cancel(input)


# 1. Send encoded input to a named RawValue handler or a dynamic Nexus handler.
# 2. Replace the RawValue in an interceptor and check that the handler receives the edit.
# This validates that interception hides LazyValue without decoding codecs twice or
# mistaking a named RawValue operation for a dynamic operation.
@pytest.mark.parametrize("operation", ["named", "dynamic"])
async def test_interceptor_replaces_raw_input(operation: str):
    class DynamicHandler(nexusrpc.handler.OperationHandler[nexusrpc.LazyValue, str]):
        async def start(
            self,
            ctx: nexusrpc.handler.StartOperationContext,
            input: nexusrpc.LazyValue,
        ) -> nexusrpc.handler.StartOperationResultSync[str]:
            return nexusrpc.handler.StartOperationResultSync(
                await input.consume(as_type=str)
            )

        async def cancel(
            self, ctx: nexusrpc.handler.CancelOperationContext, token: str
        ) -> None:
            raise AssertionError("Cancellation is not expected")

    @nexusrpc.handler.service_handler(name="tools")
    class Tools:
        @nexusrpc.handler.sync_operation
        async def named(
            self,
            _ctx: nexusrpc.handler.StartOperationContext,
            input: temporalio.common.RawValue,
        ) -> str:
            return temporalio.converter.PayloadConverter.default.from_payload(
                input.payload, str
            )

        @nexusrpc.handler.operation_handler(dynamic=True)
        def forward(self) -> nexusrpc.handler.OperationHandler[nexusrpc.LazyValue, str]:
            return DynamicHandler()

    class ModifyingInbound(NexusOperationInboundInterceptor):
        async def execute_nexus_operation_start(
            self, input: ExecuteNexusOperationStartInput
        ) -> (
            nexusrpc.handler.StartOperationResultSync[Any]
            | nexusrpc.handler.StartOperationResultAsync
        ):
            assert isinstance(input.input, temporalio.common.RawValue)
            assert (
                temporalio.converter.PayloadConverter.default.from_payload(
                    input.input.payload, str
                )
                == "original"
            )
            input.input = temporalio.common.RawValue(
                temporalio.converter.PayloadConverter.default.to_payload("intercepted")
            )
            return await self.next.execute_nexus_operation_start(input)

    class ModifyingInterceptor(Interceptor):
        def intercept_nexus_operation(
            self, next: NexusOperationInboundInterceptor
        ) -> NexusOperationInboundInterceptor:
            return ModifyingInbound(next)

    codec = mock.Mock(spec=temporalio.converter.PayloadCodec)
    codec.decode = mock.AsyncMock(side_effect=lambda payloads: list(payloads))
    data_converter = temporalio.converter.DataConverter(payload_codec=codec)
    payload = data_converter.payload_converter.to_payload("original")
    handler = nexusrpc.handler.Handler(
        [Tools()],
        middleware=[
            temporalio.worker._nexus._NexusMiddlewareForInterceptors(
                [ModifyingInterceptor()]
            )
        ],
    )
    result = await handler.start_operation(
        nexusrpc.handler.StartOperationContext(
            service="tools",
            operation=operation,
            headers={},
            request_id="request-id",
            task_cancellation=temporalio.worker._nexus._NexusTaskCancellation(),
        ),
        nexusrpc.LazyValue(
            serializer=temporalio.worker._nexus._NexusPayloadSerializer(
                data_converter=data_converter, payload=payload
            ),
            headers={},
        ),
    )
    assert isinstance(result, nexusrpc.handler.StartOperationResultSync)
    assert result.value == "intercepted"
    codec.decode.assert_awaited_once()


# 1. Instantiate a Nexus service that has a "list_tools" operation handler
#    and a dynamic handler that can handle "echo_sync" and "echo_workflow"
#    operations.
# 2. Validate that each operation gets the expected output.
# 3. Check that interceptors receive decoded named input or RawValue for the fallback.
# This parameterized test ensures that named operations (list_tools) get
# dispatched to its registered operation handler, while others get dispatched
# to the dynamic handler and correctly handled.
@pytest.mark.requires_local_server
@pytest.mark.parametrize(
    ["operation", "expected"],
    [
        ("list_tools", "manifest"),
        ("echo_sync", "sync:hello"),
        ("echo_workflow", "hello"),
        ("missing", None),
    ],
)
async def test_workflow_caller_routes_named_and_dynamic_operations(
    client: Client, env: WorkflowEnvironment, operation: str, expected: str | None
):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus requires a local server")
    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)

    @nexusrpc.handler.service_handler(name="dynamic-tools")
    class Tools:
        @nexusrpc.handler.sync_operation
        async def list_tools(
            self, _ctx: nexusrpc.handler.StartOperationContext, _input: str
        ) -> str:
            return "manifest"

        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            ctx: temporalio.nexus.TemporalStartOperationContext,
            client: temporalio.nexus.TemporalNexusClient,
            input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            value = (
                temporalio.nexus.client().data_converter.payload_converter.from_payload(
                    input.payload, str
                )
            )
            if ctx.operation == "echo_sync":
                return temporalio.nexus.TemporalOperationResult.sync(f"sync:{value}")
            if ctx.operation == "echo_workflow":
                return await client.start_workflow(
                    EchoWorkflow.run, value, id=str(uuid.uuid4())
                )
            raise nexusrpc.HandlerError(
                "Unknown tool", type=nexusrpc.HandlerErrorType.NOT_FOUND
            )

    interceptor = RecordingInterceptor()
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        workflows=[DynamicCallerWorkflow, EchoWorkflow],
        interceptors=[interceptor],
    ):
        caller = await client.start_workflow(
            DynamicCallerWorkflow.run,
            CallerInput(endpoint=endpoint, operation=operation, value="hello"),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        if expected is None:
            with pytest.raises(WorkflowFailureError) as err:
                await caller.result()
            assert isinstance(
                err.value.__cause__, temporalio.exceptions.NexusOperationError
            )
            handler_error = err.value.__cause__.__cause__
            assert isinstance(handler_error, nexusrpc.HandlerError)
            assert handler_error.type == nexusrpc.HandlerErrorType.NOT_FOUND
            assert not handler_error.retryable
        else:
            assert await caller.result() == expected
    assert interceptor.starts == [
        (operation, str if operation == "list_tools" else temporalio.common.RawValue)
    ]


# 1. Start a Workflow-backed dynamic operation from a caller Workflow.
# 2. Wait for its start, cancel the caller, and check the backing Workflow is canceled.
# This ensures that cancelation via token works as expected for workflow-backed
# operation in the dynamic handler.
@pytest.mark.requires_local_server
async def test_workflow_caller_cancels_dynamic_workflow_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus requires a local server")
    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)

    @nexusrpc.handler.service_handler(name="dynamic-tools")
    class Tools:
        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            _ctx: temporalio.nexus.TemporalStartOperationContext,
            client: temporalio.nexus.TemporalNexusClient,
            input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            return await client.start_workflow(
                WaitingWorkflow.run,
                id=temporalio.nexus.client().data_converter.payload_converter.from_payload(
                    input.payload, str
                ),
            )

    interceptor = RecordingInterceptor()
    backing_id = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        workflows=[DynamicCancelCallerWorkflow, WaitingWorkflow],
        interceptors=[interceptor],
    ):
        caller = await client.start_workflow(
            DynamicCancelCallerWorkflow.run,
            CallerInput(endpoint=endpoint, operation="wait_workflow", value=backing_id),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        await caller.execute_update(DynamicCancelCallerWorkflow.wait_started)
        await caller.cancel()
        with pytest.raises(WorkflowFailureError) as err:
            await client.get_workflow_handle(backing_id).result()
        assert isinstance(err.value.__cause__, temporalio.exceptions.CancelledError)
    assert interceptor.starts == [("wait_workflow", temporalio.common.RawValue)]
    assert interceptor.cancels == ["wait_workflow"]


# 1. Start an Activity-backed dynamic operation and cancel its pending handle.
# 2. Wait for both the Nexus operation and its backing Activity to become CANCELED.
# This ensures that cancelation via token works as expected for activity-backed
# operation in the dynamic handler.
@pytest.mark.requires_local_server
async def test_standalone_caller_cancels_dynamic_activity_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus requires a local server")
    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)

    @activity.defn
    async def wait_for_cancel() -> None:
        while True:
            await asyncio.sleep(0.3)
            activity.heartbeat()

    @nexusrpc.handler.service_handler(name="dynamic-tools")
    class Tools:
        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            _ctx: temporalio.nexus.TemporalStartOperationContext,
            client: temporalio.nexus.TemporalNexusClient,
            input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            return await client.start_activity(
                wait_for_cancel,
                id=temporalio.nexus.client().data_converter.payload_converter.from_payload(
                    input.payload, str
                ),
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=1),
            )

    interceptor = RecordingInterceptor()
    activity_id = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        activities=[wait_for_cancel],
        interceptors=[interceptor],
    ):
        nexus_client: NexusClient[Any] = client.create_nexus_client(
            "dynamic-tools", endpoint
        )
        handle: NexusOperationHandle[Any] = await nexus_client.start_operation(
            "wait_activity", activity_id, id=str(uuid.uuid4())
        )
        await handle.cancel()
        activity_handle = client.get_activity_handle(activity_id)

        async def cancelled():
            assert (
                await handle.describe()
            ).status is temporalio.common.NexusOperationExecutionStatus.CANCELED
            assert (
                await activity_handle.describe()
            ).status is ActivityExecutionStatus.CANCELED

        await assert_eventually(cancelled)
    assert interceptor.starts == [("wait_activity", temporalio.common.RawValue)]
    assert interceptor.cancels == ["wait_activity"]


# 1. Start a dynamic operation backed by an accepted, blocking Workflow Update.
# 2. Cancel the pending Nexus operation through a custom Update cancellation handler.
# 3. Check the handler receives the backing Workflow and Update IDs from the token.
# This ensures that cancelation via token works as expected for update-backed
# operation in the dynamic handler.
@pytest.mark.requires_local_server
async def test_dynamic_update_custom_cancel(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus requires a local server")
    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)
    cancelled: asyncio.Queue[temporalio.nexus.CancelUpdateWorkflowOptions] = (
        asyncio.Queue()
    )

    class UpdateHandler(
        temporalio.nexus.TemporalOperationHandler[nexusrpc.LazyValue, Any]
    ):
        async def start_operation(
            self,
            ctx: temporalio.nexus.TemporalStartOperationContext,
            client: temporalio.nexus.TemporalNexusClient,
            input: nexusrpc.LazyValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            return await client.start_workflow_update(
                await input.consume(as_type=str),
                UpdateTargetWorkflow.block,
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            )

        async def cancel_workflow_update(
            self,
            ctx: temporalio.nexus.TemporalCancelOperationContext,
            options: temporalio.nexus.CancelUpdateWorkflowOptions,
        ) -> None:
            cancelled.put_nowait(options)

    @nexusrpc.handler.service_handler(name="dynamic-tools")
    class Tools:
        @nexusrpc.handler.operation_handler(dynamic=True)
        def forward(self) -> nexusrpc.handler.OperationHandler[nexusrpc.LazyValue, Any]:
            return UpdateHandler()

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        workflows=[UpdateTargetWorkflow],
    ):
        target = await client.start_workflow(
            UpdateTargetWorkflow.run, id=str(uuid.uuid4()), task_queue=task_queue
        )
        try:
            nexus_client: NexusClient[Any] = client.create_nexus_client(
                "dynamic-tools", endpoint
            )
            pending: NexusOperationHandle[Any] = await nexus_client.start_operation(
                "block", target.id, id=str(uuid.uuid4())
            )

            async def started():
                assert (await pending.describe()).operation_token

            await assert_eventually(started)
            await pending.cancel()
            options = await asyncio.wait_for(cancelled.get(), timeout=10)
            assert options.workflow_id == target.id
            assert options.update_id
            assert options.run_id == target.result_run_id
        finally:
            await target.terminate()


# 1. Start a dynamic operation, then reject new starts and replace the Worker.
# 2. Check that the original backing Workflow or Activity is still running.
# 3. Cancel using the original pending handle and check the backing execution is CANCELED.
# This validates that cancellation needs only the token, despite the worker
# restarting with a new instance of the Nexus service.
@pytest.mark.requires_local_server
@pytest.mark.parametrize("operation", ["wait_workflow", "wait_activity"])
async def test_cancel_by_token_after_tool_removal_and_restart(
    client: Client, env: WorkflowEnvironment, operation: str
):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus requires a local server")
    task_queue = str(uuid.uuid4())
    endpoint = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint, task_queue)
    available = True

    @activity.defn
    async def wait_for_cancel() -> None:
        while True:
            await asyncio.sleep(0.3)
            activity.heartbeat()

    @nexusrpc.handler.service_handler(name="dynamic-tools")
    class Tools:
        @temporalio.nexus.temporal_operation(dynamic=True)
        async def forward(
            self,
            ctx: temporalio.nexus.TemporalStartOperationContext,
            client: temporalio.nexus.TemporalNexusClient,
            input: temporalio.common.RawValue,
        ) -> temporalio.nexus.TemporalOperationResult[Any]:
            if ctx.operation != operation or not available:
                raise nexusrpc.HandlerError(
                    "Unknown tool", type=nexusrpc.HandlerErrorType.NOT_FOUND
                )
            backing_id = (
                temporalio.nexus.client().data_converter.payload_converter.from_payload(
                    input.payload, str
                )
            )
            if operation == "wait_workflow":
                return await client.start_workflow(WaitingWorkflow.run, id=backing_id)
            return await client.start_activity(
                wait_for_cancel,
                id=backing_id,
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=1),
            )

    nexus_client: NexusClient[Any] = client.create_nexus_client(
        "dynamic-tools", endpoint
    )
    backing_id = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        workflows=[WaitingWorkflow] if operation == "wait_workflow" else [],
        activities=[wait_for_cancel] if operation == "wait_activity" else [],
    ):
        pending: NexusOperationHandle[Any] = await nexus_client.start_operation(
            operation, backing_id, id=backing_id
        )

        async def started():
            assert (await pending.describe()).operation_token

        await assert_eventually(started)
        available = False
        rejected: NexusOperationHandle[Any] = await nexus_client.start_operation(
            operation, str(uuid.uuid4()), id=str(uuid.uuid4())
        )
        with pytest.raises(NexusOperationFailureError) as err:
            await rejected.result()
        assert isinstance(err.value.__cause__, nexusrpc.HandlerError)
        assert err.value.__cause__.type == nexusrpc.HandlerErrorType.NOT_FOUND

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[Tools()],
        workflows=[WaitingWorkflow] if operation == "wait_workflow" else [],
        activities=[wait_for_cancel] if operation == "wait_activity" else [],
    ):
        assert (
            await pending.describe()
        ).status is temporalio.common.NexusOperationExecutionStatus.RUNNING
        if operation == "wait_workflow":
            assert (
                await client.get_workflow_handle(backing_id).describe()
            ).status is WorkflowExecutionStatus.RUNNING
        else:
            assert (
                await client.get_activity_handle(backing_id).describe()
            ).status is ActivityExecutionStatus.RUNNING

        await pending.cancel()

        async def cancelled():
            if operation == "wait_workflow":
                assert (
                    await client.get_workflow_handle(backing_id).describe()
                ).status is WorkflowExecutionStatus.CANCELED
            else:
                assert (
                    await client.get_activity_handle(backing_id).describe()
                ).status is ActivityExecutionStatus.CANCELED

        await assert_eventually(cancelled)
