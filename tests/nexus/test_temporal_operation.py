import asyncio
import uuid
from dataclasses import dataclass

import nexusrpc
import pytest
from nexusrpc import HandlerErrorType, Operation, service
from nexusrpc.handler import operation_handler, service_handler
from typing_extensions import override

import temporalio.exceptions
from temporalio import nexus, workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import NexusOperationExecutionStatus, WorkflowIDConflictPolicy
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


@service
class TestService:
    echo: Operation[Input, str]
    blocking: Operation[None, None]
    double_start: Operation[Input, None]
    concurrent_start: Operation[Input, str]
    retry_after_failed_start: Operation[Input, str]
    sync_result: Operation[Input, str]
    custom_cancel: Operation[str, None]


@service_handler(service=TestService)
class TestServiceHandler:
    # tell Pytest this is not a test class
    __test__ = False

    def __init__(self) -> None:
        self.started_custom_cancel_workflow = asyncio.Event()

    @nexus.temporal_operation
    async def echo(
        self,
        _ctx: nexus.TemporalNexusStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_workflow(
            EchoWorkflow.run, input, id=f"echo-{input.value}"
        )

    @nexus.temporal_operation
    async def blocking(
        self,
        _ctx: nexus.TemporalNexusStartOperationContext,
        client: nexus.TemporalNexusClient,
        _input: None,
    ) -> nexus.TemporalOperationResult[None]:
        return await client.start_workflow(
            BlockingWorkflow.run, id=f"blocking-{uuid.uuid4()}"
        )

    @nexus.temporal_operation
    async def double_start(
        self,
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
        _client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        return nexus.TemporalOperationResult.sync(input.value)

    @operation_handler
    def custom_cancel(self) -> nexus.TemporalNexusOperationHandler[str, None]:
        event = self.started_custom_cancel_workflow

        class CustomCancelNexusOpHandler(
            nexus.TemporalNexusOperationHandler[str, None]
        ):
            @override
            async def start_operation(
                self,
                ctx: nexus.TemporalNexusStartOperationContext,
                client: nexus.TemporalNexusClient,
                input: str,
            ) -> nexus.TemporalOperationResult[None]:
                result = await client.start_workflow(BlockingWorkflow.run, id=input)
                event.set()
                return result

            @override
            async def cancel_workflow_run(
                self,
                ctx: nexus.TemporalNexusCancelOperationContext,
                options: nexus.CancelWorkflowRunOptions,
            ):
                # get a handle to the workflow
                handle = nexus.client().get_workflow_handle(options.workflow_id)

                # cancel the workflow
                await handle.cancel()

        return CustomCancelNexusOpHandler()


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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
        _ctx: nexus.TemporalNexusStartOperationContext,
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
