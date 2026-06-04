import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import nexusrpc
import pytest
from nexusrpc import HandlerErrorType, Operation, service
from nexusrpc.handler import (
    CancelOperationContext,
    OperationTaskCancellation,
    operation_handler,
    service_handler,
)
from typing_extensions import override

import temporalio.exceptions
from temporalio import activity, nexus, workflow
from temporalio.client import (
    ActivityExecutionStatus,
    Client,
    NexusOperationFailureError,
    WorkflowExecutionStatus,
    WorkflowFailureError,
)
from temporalio.common import (
    NexusOperationExecutionStatus,
    RetryPolicy,
    WorkflowIDConflictPolicy,
)
from temporalio.nexus._token import OperationToken, OperationTokenType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import EventType, assert_event_subsequence, assert_eventually
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class Input:
    value: str
    task_queue: str


def test_temporal_operation_result_validates_single_result_kind() -> None:
    assert nexus.TemporalOperationResult.sync(None).value is None
    assert nexus.TemporalOperationResult.async_token("token").token == "token"

    with pytest.raises(ValueError, match="exactly one of value or token"):
        nexus.TemporalOperationResult()

    with pytest.raises(ValueError, match="exactly one of value or token"):
        nexus.TemporalOperationResult(value="value", token="token")


def test_temporal_operation_result_validates_token() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        nexus.TemporalOperationResult.async_token("")

    with pytest.raises(ValueError, match="non-empty string"):
        nexus.TemporalOperationResult(token="")

    with pytest.raises(ValueError, match="non-empty string"):
        nexus.TemporalOperationResult(token=123)  # type: ignore


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: Input) -> str:
        return input.value


@activity.defn
async def echo_activity(input: Input) -> str:
    return input.value


@activity.defn
async def raise_error_activity() -> None:
    raise temporalio.exceptions.ApplicationError(
        "test-activity-error-message",
        type="test-activity-error-type",
        non_retryable=True,
    )


@activity.defn
async def wait_for_cancel_activity() -> None:
    # Heartbeat in a loop so the activity receives cancellation. Letting the
    # resulting CancelledError bubble out transitions the activity to CANCELED.
    while True:
        await asyncio.sleep(0.3)
        activity.heartbeat()


@service
class TestService:
    echo: Operation[Input, str]
    blocking: Operation[None, None]
    double_start: Operation[Input, None]
    concurrent_start: Operation[Input, str]
    retry_after_failed_start: Operation[Input, str]
    sync_result: Operation[Input, str]
    custom_cancel: Operation[str, None]
    echo_activity: Operation[Input, str]
    error_activity: Operation[Input, None]
    blocking_activity: Operation[str, None]
    custom_cancel_activity: Operation[str, None]
    double_start_activity: Operation[Input, None]
    mixed_start: Operation[Input, None]


@service_handler(service=TestService)
class TestServiceHandler:
    # tell Pytest this is not a test class
    __test__ = False

    def __init__(self) -> None:
        self.started_custom_cancel_workflow = asyncio.Event()
        self.started_custom_cancel_activity = asyncio.Event()
        self.custom_cancel_activity_called = asyncio.Event()

    @nexus.temporal_operation
    async def echo(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_workflow(
            EchoWorkflow.run, input, id=f"echo-{input.value}"
        )

    @nexus.temporal_operation
    async def blocking(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        _input: None,
    ) -> nexus.TemporalOperationResult[None]:
        return await client.start_workflow(
            BlockingWorkflow.run, id=f"blocking-{uuid.uuid4()}"
        )

    @nexus.temporal_operation
    async def double_start(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[None]:
        await client.start_workflow(
            EchoWorkflow.run, input, id=f"double-start-{uuid.uuid4()}"
        )
        await client.start_workflow(
            EchoWorkflow.run, input, id=f"double-start-{uuid.uuid4()}"
        )
        return nexus.TemporalOperationResult.sync(None)

    @nexus.temporal_operation
    async def concurrent_start(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        results = await asyncio.gather(
            client.start_workflow(
                EchoWorkflow.run,
                input,
                id=f"concurrent-start-1-{uuid.uuid4()}",
            ),
            client.start_workflow(
                EchoWorkflow.run,
                input,
                id=f"concurrent-start-2-{uuid.uuid4()}",
            ),
            return_exceptions=True,
        )

        async_results: list[nexus.TemporalOperationResult[str]] = []
        handler_errors: list[nexusrpc.HandlerError] = []
        for result in results:
            if isinstance(result, nexus.TemporalOperationResult):
                async_results.append(result)
            elif isinstance(result, nexusrpc.HandlerError):
                handler_errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                raise RuntimeError(f"Unexpected concurrent start result: {result}")

        if (
            len(async_results) == 1
            and len(handler_errors) == 1
            and handler_errors[0].type == HandlerErrorType.BAD_REQUEST
        ):
            return async_results[0]

        raise RuntimeError(
            "Expected one async workflow start and one BAD_REQUEST HandlerError, "
            f"got {len(async_results)} starts and {len(handler_errors)} handler errors"
        )

    @nexus.temporal_operation
    async def retry_after_failed_start(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        try:
            await client.start_workflow(
                BlockingWorkflow.run,
                id=input.value,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except temporalio.exceptions.WorkflowAlreadyStartedError:
            return await client.start_workflow(
                EchoWorkflow.run,
                input,
                id=f"retry-after-failed-start-{uuid.uuid4()}",
            )

        raise RuntimeError("Expected first workflow start to fail")

    @nexus.temporal_operation
    async def sync_result(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        _client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        return nexus.TemporalOperationResult.sync(input.value)

    @operation_handler
    def custom_cancel(self) -> nexus.TemporalOperationHandler[str, None]:
        event = self.started_custom_cancel_workflow

        class CustomCancelNexusOpHandler(nexus.TemporalOperationHandler[str, None]):
            @override
            async def start_operation(
                self,
                ctx: nexus.TemporalStartOperationContext,
                client: nexus.TemporalNexusClient,
                input: str,
            ) -> nexus.TemporalOperationResult[None]:
                result = await client.start_workflow(BlockingWorkflow.run, id=input)
                event.set()
                return result

            @override
            async def cancel_workflow_run(
                self,
                ctx: nexus.TemporalCancelOperationContext,
                options: nexus.CancelWorkflowRunOptions,
            ):
                # get a handle to the workflow
                handle = nexus.client().get_workflow_handle(options.workflow_id)

                # cancel the workflow
                await handle.cancel()

        return CustomCancelNexusOpHandler()

    @nexus.temporal_operation
    async def echo_activity(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_activity(
            echo_activity,
            input,
            id=f"echo_activity-{uuid.uuid4()}",
            start_to_close_timeout=timedelta(seconds=5),
        )

    @nexus.temporal_operation
    async def error_activity(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        _input: Input,
    ) -> nexus.TemporalOperationResult[None]:
        # The activity raises immediately. With a single permitted attempt it
        # fails the backing activity, which in turn fails the Nexus operation.
        return await client.start_activity(
            raise_error_activity,
            id=f"error_activity-{uuid.uuid4()}",
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    @nexus.temporal_operation
    async def blocking_activity(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: str,
    ) -> nexus.TemporalOperationResult[None]:
        return await client.start_activity(
            wait_for_cancel_activity,
            id=input,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=1),
        )

    @nexus.temporal_operation
    async def double_start_activity(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[None]:
        await client.start_activity(
            echo_activity,
            input,
            id=f"double-start-activity-{uuid.uuid4()}",
            start_to_close_timeout=timedelta(seconds=5),
        )
        await client.start_activity(
            echo_activity,
            input,
            id=f"double-start-activity-{uuid.uuid4()}",
            start_to_close_timeout=timedelta(seconds=5),
        )
        return nexus.TemporalOperationResult.sync(None)

    @nexus.temporal_operation
    async def mixed_start(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[None]:
        # Starting a workflow reserves the single async start, so the subsequent
        # start_activity must hit the same guard and raise a BAD_REQUEST error.
        await client.start_workflow(
            EchoWorkflow.run, input, id=f"mixed-start-{uuid.uuid4()}"
        )
        await client.start_activity(
            echo_activity,
            input,
            id=f"mixed-start-{uuid.uuid4()}",
            start_to_close_timeout=timedelta(seconds=5),
        )
        return nexus.TemporalOperationResult.sync(None)

    @operation_handler
    def custom_cancel_activity(self) -> nexus.TemporalOperationHandler[str, None]:
        started = self.started_custom_cancel_activity
        cancel_called = self.custom_cancel_activity_called

        class CustomCancelActivityNexusOpHandler(
            nexus.TemporalOperationHandler[str, None]
        ):
            @override
            async def start_operation(
                self,
                ctx: nexus.TemporalStartOperationContext,
                client: nexus.TemporalNexusClient,
                input: str,
            ) -> nexus.TemporalOperationResult[None]:
                result = await client.start_activity(
                    wait_for_cancel_activity,
                    id=input,
                    start_to_close_timeout=timedelta(seconds=30),
                    heartbeat_timeout=timedelta(seconds=1),
                )
                started.set()
                return result

            @override
            async def cancel_activity(
                self,
                ctx: nexus.TemporalCancelOperationContext,
                options: nexus.CancelActivityOptions,
            ):
                # record that the custom override ran
                cancel_called.set()

                # get a handle to the activity and cancel it
                handle = nexus.client().get_activity_handle(options.activity_id)
                await handle.cancel()

        return CustomCancelActivityNexusOpHandler()


@workflow.defn
class EchoWorkflowCaller:
    @workflow.run
    async def run(self, input: Input) -> str:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        return await client.execute_operation(TestService.echo, input)


async def test_temporal_operation_start_workflow(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[EchoWorkflow, EchoWorkflowCaller],
    ):
        wf_handle = await client.start_workflow(
            EchoWorkflowCaller.run,
            Input(value="test", task_queue=task_queue),
            task_queue=task_queue,
            id=str(uuid.uuid4()),
        )
        result = await wf_handle.result()
        assert result == "test"

        await assert_event_subsequence(
            wf_handle,
            [
                EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
            ],
        )


async def test_temporal_operation_cancel_rejects_unknown_tokens():
    class FakeNexusTaskCancellation(OperationTaskCancellation):
        def is_cancelled(self) -> bool:
            return False

        def cancellation_reason(self) -> str | None:
            return None

        def wait_until_cancelled_sync(self, timeout: float | None = None) -> bool:
            return False

        async def wait_until_cancelled(self) -> None:
            return None

        def cancel(self, _reason: str) -> bool:
            return False

    cancel_ctx = CancelOperationContext(
        service="TestService",
        operation="echo",
        headers={},
        task_cancellation=FakeNexusTaskCancellation(),
    )

    service_handler = TestServiceHandler()

    # Use a factory style operation form the handler to allow calling cancel directly
    op_handler = service_handler.custom_cancel()

    # Invalid token type
    token = OperationToken(type=30, namespace="default")  # type: ignore
    with pytest.raises(nexusrpc.HandlerError) as err:
        await op_handler.cancel(cancel_ctx, token.encode())
    assert err.value.type == HandlerErrorType.INTERNAL
    assert not err.value.retryable
    underlying = err.value.__cause__
    assert isinstance(underlying, TypeError)
    assert "unknown token type, got 30" in str(underlying)

    # Workflow ID missing from workflow type
    token = OperationToken(type=OperationTokenType.WORKFLOW, namespace="default")
    with pytest.raises(nexusrpc.HandlerError) as err:
        await op_handler.cancel(cancel_ctx, token.encode())
    assert err.value.type == HandlerErrorType.INTERNAL
    assert not err.value.retryable
    underlying = err.value.__cause__
    assert isinstance(underlying, TypeError)
    assert "expected non-empty workflow id for token type `WORKFLOW`" in str(underlying)

    # Activity ID missing from activity type
    token = OperationToken(type=OperationTokenType.ACTIVITY, namespace="default")
    with pytest.raises(nexusrpc.HandlerError) as err:
        await op_handler.cancel(cancel_ctx, token.encode())
    assert err.value.type == HandlerErrorType.INTERNAL
    assert not err.value.retryable
    underlying = err.value.__cause__
    assert isinstance(underlying, TypeError)
    assert "expected non-empty activity id for token type `ACTIVITY`" in str(underlying)


@workflow.defn
class BlockingWorkflow:
    def __init__(self) -> None:
        self.done: bool = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.done)

    @workflow.update
    async def unblock(self):
        self.done = True


@workflow.defn
class CancelBlockingWorkflowCaller:
    op_started = False

    @workflow.run
    async def run(self, input: Input) -> None:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        op_handle = await client.start_operation(TestService.blocking, None)
        self.op_started = True
        return await op_handle

    @workflow.update
    async def wait_operation_started(self):
        await workflow.wait_condition(lambda: self.op_started)


async def test_temporal_operation_cancel_workflow(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[BlockingWorkflow, CancelBlockingWorkflowCaller],
    ):
        wf_handle = await client.start_workflow(
            CancelBlockingWorkflowCaller.run,
            Input(value="test", task_queue=task_queue),
            task_queue=task_queue,
            id=f"blocking-{uuid.uuid4()}",
        )

        await wf_handle.execute_update(
            CancelBlockingWorkflowCaller.wait_operation_started
        )

        await wf_handle.cancel()

        await assert_event_subsequence(
            wf_handle,
            [
                EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUESTED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_CANCEL_REQUEST_COMPLETED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_CANCELED,
            ],
        )


async def test_customized_temporal_operation_cancel_workflow(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)

    service_handler = TestServiceHandler()
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
        workflows=[BlockingWorkflow, CancelBlockingWorkflowCaller],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        wf_id = f"custom-cancel-{uuid.uuid4()}"
        op_handle = await nexus_client.start_operation(
            TestService.custom_cancel, wf_id, id=str(uuid.uuid4())
        )

        await service_handler.started_custom_cancel_workflow.wait()

        await op_handle.cancel()

        async def check_cancelled():
            wf_handle = client.get_workflow_handle(wf_id)
            wf_desc = await wf_handle.describe()
            assert wf_desc.status is WorkflowExecutionStatus.CANCELED
            op_desc = await op_handle.describe()
            assert op_desc.status is NexusOperationExecutionStatus.CANCELED

        await assert_eventually(check_cancelled)


@workflow.defn
class DoubleStartWorkflowCaller:
    @workflow.run
    async def run(self, input: Input) -> None:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        op_handle = await client.start_operation(TestService.double_start, input)
        return await op_handle


@workflow.defn
class ConcurrentStartWorkflowCaller:
    @workflow.run
    async def run(self, input: Input) -> str:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        return await client.execute_operation(TestService.concurrent_start, input)


@workflow.defn
class FailedStartRollbackWorkflowCaller:
    @workflow.run
    async def run(self, input: Input) -> str:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        return await client.execute_operation(
            TestService.retry_after_failed_start,
            input,
        )


async def test_temporal_operation_double_start_raises_handler_err(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[EchoWorkflow, DoubleStartWorkflowCaller],
    ):
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                DoubleStartWorkflowCaller.run,
                Input(value="test", task_queue=task_queue),
                task_queue=task_queue,
                id=f"double-start-{uuid.uuid4()}",
            )

        assert isinstance(err.value.cause, temporalio.exceptions.NexusOperationError)
        assert isinstance(err.value.cause.cause, nexusrpc.HandlerError)
        assert err.value.cause.cause.type == HandlerErrorType.BAD_REQUEST
        assert (
            "Only one async operation can be started per operation handler invocation"
            in err.value.cause.cause.message
        )


async def test_temporal_operation_concurrent_start_raises_handler_err(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[EchoWorkflow, ConcurrentStartWorkflowCaller],
    ):
        result = await client.execute_workflow(
            ConcurrentStartWorkflowCaller.run,
            Input(value="test", task_queue=task_queue),
            task_queue=task_queue,
            id=f"concurrent-start-{uuid.uuid4()}",
        )

        assert result == "test"


async def test_temporal_operation_failed_start_allows_retry(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    conflict_id = f"failed-start-rollback-{uuid.uuid4()}"
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[
            BlockingWorkflow,
            EchoWorkflow,
            FailedStartRollbackWorkflowCaller,
        ],
    ):
        conflict_handle = await client.start_workflow(
            BlockingWorkflow.run,
            id=conflict_id,
            task_queue=task_queue,
        )

        try:
            result = await client.execute_workflow(
                FailedStartRollbackWorkflowCaller.run,
                Input(value=conflict_id, task_queue=task_queue),
                task_queue=task_queue,
                id=f"failed-start-rollback-caller-{uuid.uuid4()}",
            )
            assert result == conflict_id
        finally:
            await conflict_handle.cancel()


async def test_temporal_operation_mixed_start_raises_handler_err(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[EchoWorkflow],
        activities=[echo_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        with pytest.raises(NexusOperationFailureError) as err:
            await nexus_client.execute_operation(
                TestService.mixed_start,
                Input(value="test", task_queue=task_queue),
                id=str(uuid.uuid4()),
            )

        assert isinstance(err.value.cause, nexusrpc.HandlerError)
        assert err.value.cause.type == HandlerErrorType.BAD_REQUEST
        assert (
            "Only one async operation can be started per operation handler invocation"
            in err.value.cause.message
        )


@workflow.defn
class SyncResultCaller:
    @workflow.run
    async def run(self, input: Input) -> str:
        client = workflow.create_nexus_client(
            service=TestService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )
        return await client.execute_operation(TestService.sync_result, input)


async def test_temporal_operation_sync_result(client: Client, env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[SyncResultCaller],
    ):
        wf_handle = await client.start_workflow(
            SyncResultCaller.run,
            Input(value="test", task_queue=task_queue),
            task_queue=task_queue,
            id=str(uuid.uuid4()),
        )
        result = await wf_handle.result()
        assert result == "test"

        # Sync results do not produce a NEXUS_OPERATION_STARTED event,
        await assert_event_subsequence(
            wf_handle,
            [
                EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
            ],
        )


async def test_temporal_operation_start_activity(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        activities=[echo_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        result = await nexus_client.execute_operation(
            TestService.echo_activity,
            Input(value="test", task_queue=task_queue),
            id=str(uuid.uuid4()),
        )
        assert result == "test"


async def test_temporal_operation_start_activity_raises_error(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        activities=[raise_error_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        with pytest.raises(NexusOperationFailureError) as err:
            await nexus_client.execute_operation(
                TestService.error_activity,
                Input(value="test", task_queue=task_queue),
                id=str(uuid.uuid4()),
            )

        operation_err = err.value.__cause__
        assert isinstance(operation_err, temporalio.exceptions.ApplicationError)
        assert operation_err.type == "OperationError"
        assert "nexus operation completed unsuccessfully" in str(operation_err)

        application_err = operation_err.__cause__
        assert isinstance(application_err, temporalio.exceptions.ApplicationError)

        assert application_err.type == "test-activity-error-type"
        assert "test-activity-error-message" in str(application_err)
        assert application_err.__cause__ is None


async def test_temporal_operation_cancel_activity(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        activities=[wait_for_cancel_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        activity_id = f"blocking-activity-{uuid.uuid4()}"
        op_handle = await nexus_client.start_operation(
            TestService.blocking_activity, activity_id, id=str(uuid.uuid4())
        )

        await op_handle.cancel()

        activity_handle = client.get_activity_handle(activity_id)

        async def check_cancelled():
            op_desc = await op_handle.describe()
            assert op_desc.status is NexusOperationExecutionStatus.CANCELED
            activity_desc = await activity_handle.describe()
            assert activity_desc.status is ActivityExecutionStatus.CANCELED

        await assert_eventually(check_cancelled)


async def test_customized_temporal_operation_cancel_activity(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)

    service_handler = TestServiceHandler()
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
        activities=[wait_for_cancel_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        activity_id = f"custom-cancel-activity-{uuid.uuid4()}"
        op_handle = await nexus_client.start_operation(
            TestService.custom_cancel_activity, activity_id, id=str(uuid.uuid4())
        )
        await service_handler.started_custom_cancel_activity.wait()

        await op_handle.cancel()

        activity_handle = client.get_activity_handle(activity_id)

        async def check_cancelled():
            assert service_handler.custom_cancel_activity_called.is_set()
            op_desc = await op_handle.describe()
            assert op_desc.status is NexusOperationExecutionStatus.CANCELED
            activity_desc = await activity_handle.describe()
            assert activity_desc.status is ActivityExecutionStatus.CANCELED

        await assert_eventually(check_cancelled)


async def test_temporal_operation_double_start_activity_raises_handler_err(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        activities=[echo_activity],
    ):
        nexus_client = client.create_nexus_client(TestService, endpoint_name)

        with pytest.raises(NexusOperationFailureError) as err:
            await nexus_client.execute_operation(
                TestService.double_start_activity,
                Input(value="test", task_queue=task_queue),
                id=str(uuid.uuid4()),
            )

        assert isinstance(err.value.cause, nexusrpc.HandlerError)
        assert err.value.cause.type == HandlerErrorType.BAD_REQUEST
        assert (
            "Only one async operation can be started per operation handler invocation"
            in err.value.cause.message
        )


@dataclass
class TemporalOperationOverloadTestValue:
    value: int


@workflow.defn
class TemporalOperationOverloadTestWorkflow:
    @workflow.run
    async def run(
        self, input: TemporalOperationOverloadTestValue
    ) -> TemporalOperationOverloadTestValue:
        return TemporalOperationOverloadTestValue(value=input.value * 2)


@workflow.defn
class TemporalOperationOverloadTestWorkflowNoParam:
    @workflow.run
    async def run(self) -> TemporalOperationOverloadTestValue:
        return TemporalOperationOverloadTestValue(value=0)


@service_handler
class TemporalOperationOverloadTestServiceHandler:
    @nexus.temporal_operation
    async def no_param(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        _input: TemporalOperationOverloadTestValue,
    ) -> nexus.TemporalOperationResult[TemporalOperationOverloadTestValue]:
        return await client.start_workflow(
            TemporalOperationOverloadTestWorkflowNoParam.run,
            id=str(uuid.uuid4()),
        )

    @nexus.temporal_operation
    async def single_param(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: TemporalOperationOverloadTestValue,
    ) -> nexus.TemporalOperationResult[TemporalOperationOverloadTestValue]:
        return await client.start_workflow(
            TemporalOperationOverloadTestWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )

    @nexus.temporal_operation
    async def multi_param(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: TemporalOperationOverloadTestValue,
    ) -> nexus.TemporalOperationResult[TemporalOperationOverloadTestValue]:
        return await client.start_workflow(
            TemporalOperationOverloadTestWorkflow.run,
            args=[input],
            id=str(uuid.uuid4()),
        )

    @nexus.temporal_operation
    async def by_name(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: TemporalOperationOverloadTestValue,
    ) -> nexus.TemporalOperationResult[TemporalOperationOverloadTestValue]:
        return await client.start_workflow(
            "TemporalOperationOverloadTestWorkflow",
            input,
            id=str(uuid.uuid4()),
            result_type=TemporalOperationOverloadTestValue,
        )

    @nexus.temporal_operation
    async def by_name_multi_param(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: TemporalOperationOverloadTestValue,
    ) -> nexus.TemporalOperationResult[TemporalOperationOverloadTestValue]:
        return await client.start_workflow(
            "TemporalOperationOverloadTestWorkflow",
            args=[input],
            id=str(uuid.uuid4()),
            result_type=TemporalOperationOverloadTestValue,
        )


@workflow.defn
class TemporalOperationOverloadTestCallerWorkflow:
    @workflow.run
    async def run(
        self, op: str, input: TemporalOperationOverloadTestValue
    ) -> TemporalOperationOverloadTestValue:
        client = workflow.create_nexus_client(
            service=TemporalOperationOverloadTestServiceHandler,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

        if op == "no_param":
            return await client.execute_operation(
                TemporalOperationOverloadTestServiceHandler.no_param, input
            )
        elif op == "single_param":
            return await client.execute_operation(
                TemporalOperationOverloadTestServiceHandler.single_param, input
            )
        elif op == "multi_param":
            return await client.execute_operation(
                TemporalOperationOverloadTestServiceHandler.multi_param, input
            )
        elif op == "by_name":
            return await client.execute_operation(
                TemporalOperationOverloadTestServiceHandler.by_name, input
            )
        elif op == "by_name_multi_param":
            return await client.execute_operation(
                TemporalOperationOverloadTestServiceHandler.by_name_multi_param, input
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
async def test_temporal_operation_overloads(
    client: Client, env: WorkflowEnvironment, op: str
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[
            TemporalOperationOverloadTestCallerWorkflow,
            TemporalOperationOverloadTestWorkflow,
            TemporalOperationOverloadTestWorkflowNoParam,
        ],
        nexus_service_handlers=[TemporalOperationOverloadTestServiceHandler()],
    ):
        result = await client.execute_workflow(
            TemporalOperationOverloadTestCallerWorkflow.run,
            args=[op, TemporalOperationOverloadTestValue(value=2)],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == (
            TemporalOperationOverloadTestValue(value=0)
            if op == "no_param"
            else TemporalOperationOverloadTestValue(value=4)
        )


async def test_temporal_operation_includes_workflow_token_in_callback(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[EchoWorkflow, EchoWorkflowCaller],
    ):
        input_value = f"test-{uuid.uuid4()}"
        wf_handle = await client.start_workflow(
            EchoWorkflowCaller.run,
            Input(value=input_value, task_queue=task_queue),
            task_queue=task_queue,
            id=str(uuid.uuid4()),
        )
        result = await wf_handle.result()
        assert result == input_value

        target_handle = client.get_workflow_handle(f"echo-{input_value}")

        desc = await target_handle.describe()
        token = desc.raw_description.callbacks[0].callback.nexus.header[
            "nexus-operation-token"
        ]

        expected_token = OperationToken(
            type=OperationTokenType.WORKFLOW,
            namespace=client.namespace,
            workflow_id=target_handle.id,
        ).encode()

        assert token == expected_token


async def test_temporal_operation_includes_activity_token_in_callback(
    client: Client, env: WorkflowEnvironment
):
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)

    @service_handler
    class ActivityTokenHandler:
        @nexus.temporal_operation
        async def echo_activity(
            self,
            _ctx: nexus.TemporalStartOperationContext,
            client: nexus.TemporalNexusClient,
            input: Input,
        ) -> nexus.TemporalOperationResult[str]:
            return await client.start_activity(
                echo_activity,
                input,
                id=input.value,
                start_to_close_timeout=timedelta(seconds=10),
            )

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[ActivityTokenHandler()],
        activities=[echo_activity],
    ):
        input_value = f"test-{uuid.uuid4()}"

        nexus_client = client.create_nexus_client(ActivityTokenHandler, endpoint_name)

        result = await nexus_client.execute_operation(
            ActivityTokenHandler.echo_activity,
            Input(value=input_value, task_queue=task_queue),
            id=str(uuid.uuid4()),
        )
        assert result == input_value

        activity_handle = client.get_activity_handle(input_value)

        desc = await activity_handle.describe()
        token = desc.raw_callbacks[0].info.callback.nexus.header[
            "nexus-operation-token"
        ]

        expected_token = OperationToken(
            type=OperationTokenType.ACTIVITY,
            namespace=client.namespace,
            activity_id=input_value,
        ).encode()

        assert token == expected_token
