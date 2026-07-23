import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import nexusrpc
import pytest
from nexusrpc import HandlerErrorType, Operation, service
from nexusrpc.handler import operation_handler, service_handler
from typing_extensions import override

import temporalio.exceptions
from temporalio import nexus, workflow
from temporalio.api.common.v1 import Link
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import NexusOperationExecutionStatus, WorkflowIDConflictPolicy
from temporalio.nexus._token import OperationToken, OperationTokenType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import EventType, assert_event_subsequence, assert_eventually
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class Input:
    value: str
    task_queue: str
    update_value: str = ""
    update_id: str = ""
    expect_sync_response: bool = False


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
    update_op: Operation[Input, str]


@service_handler(service=TestService)
class TestServiceHandler:
    # tell Pytest this is not a test class
    __test__ = False

    def __init__(self) -> None:
        self.started_custom_cancel_workflow = asyncio.Event()

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
    async def update_op(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: Input,
    ) -> nexus.TemporalOperationResult[str]:
        # input.value carries the target workflow_id, input.update_value has actual update
        return await client.start_workflow_update(
            input.value,
            UpdatableWorkflow.do_update,
            input.update_value,
            update_id=input.update_id,
        )


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


async def test_temporal_operation_update_workflow(
    client: Client, env: WorkflowEnvironment
) -> None:
    if (
        env.supports_time_skipping
    ):  # time skipping server uses different dynamic configs
        pytest.skip("Update workflow tests don't work with time-skipping server")
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[UpdatableWorkflow, UpdateWorkflowCaller],
    ):
        update_workflow_id = f"updatable-workflow-{uuid.uuid4()}"
        target_handle = await client.start_workflow(
            UpdatableWorkflow.run, id=update_workflow_id, task_queue=task_queue
        )

        async def check_simple_update_and_links():
            """Run an update, check state changes from pending to created, verify forward and back links are correct"""
            wf_handle = await client.start_workflow(
                UpdateWorkflowCaller.run,
                Input(
                    value=update_workflow_id,
                    task_queue=task_queue,
                    update_value="Created",
                ),
                task_queue=task_queue,
                id=f"update-workflow-caller-created-{uuid.uuid4()}",
            )
            result = await wf_handle.result()
            assert result == "Updated workflow status from Pending to Created"
            # assert expected events are in expected sequence in caller history
            await assert_event_subsequence(
                wf_handle,
                [
                    EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                    EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED,
                    EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
                ],
            )
            # now, check the links
            caller_history = await wf_handle.fetch_history()
            handler_history = await target_handle.fetch_history()
            scheduled_event = next(
                e
                for e in caller_history.events
                if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
            )
            caller_request_id = (
                scheduled_event.nexus_operation_scheduled_event_attributes.request_id
            )
            assert target_handle.result_run_id is not None
            # from caller ns to target ns
            expected_forward_link = Link(
                workflow_event=Link.WorkflowEvent(
                    namespace=client.namespace,
                    workflow_id=update_workflow_id,
                    run_id=target_handle.result_run_id,
                    request_id_ref=Link.WorkflowEvent.RequestIdReference(
                        request_id=caller_request_id,
                        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED,
                    ),
                )
            )
            assert wf_handle.result_run_id is not None
            # from target ns back to caller ns
            expected_backward_link = Link(
                workflow_event=Link.WorkflowEvent(
                    namespace=client.namespace,
                    workflow_id=wf_handle.id,
                    run_id=wf_handle.result_run_id,
                    event_ref=Link.WorkflowEvent.EventReference(
                        event_id=scheduled_event.event_id,
                        event_type=EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                    ),
                )
            )
            caller_links = [
                link
                for e in caller_history.events
                if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED
                for link in e.links
            ]
            handler_links = [
                link
                for e in handler_history.events
                if e.event_type
                == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
                for link in e.links
            ]
            assert expected_forward_link in caller_links
            assert expected_backward_link in handler_links

        async def check_sequential_updates_consistent():
            """Run updates back-to-back, verify update isnt re-processed"""
            stable_update_id = "sequential-update"
            wf_handle = await client.start_workflow(
                UpdateWorkflowCaller.run,
                Input(
                    value=update_workflow_id,
                    task_queue=task_queue,
                    update_value="Processed",
                    update_id=stable_update_id,
                ),
                task_queue=task_queue,
                id="sequential-update-workflow-caller-processed-0",
            )
            result = await wf_handle.result()
            assert result == "Updated workflow status from Created to Processed"

            # same update_id -> wont be processed again, receives a sync result
            wf_handle = await client.start_workflow(
                UpdateWorkflowCaller.run,
                Input(
                    value=update_workflow_id,
                    task_queue=task_queue,
                    update_value="Processed",
                    update_id=stable_update_id,
                    expect_sync_response=True,
                ),
                task_queue=task_queue,
                id="sequential-update-workflow-caller-processed-1",
            )
            result = await wf_handle.result()
            assert result == "Updated workflow status from Created to Processed"

        async def check_parallel_updates_idempotent_and_finish():
            """Run multiple updates in parallel, verify they are idempotent and finish with same result"""
            stable_id = "parallel-updates-id"
            num_parallel = 3
            gate = asyncio.Event()

            async def run_update(i: int) -> str:
                await gate.wait()
                wf_handle = await client.start_workflow(
                    UpdateWorkflowCaller.run,
                    Input(
                        value=update_workflow_id,
                        task_queue=task_queue,
                        update_value="Completed",
                        update_id=stable_id,
                    ),
                    task_queue=task_queue,
                    id=f"parallel-update-workflow-caller-completed-{i}",
                )
                return await wf_handle.result()

            tasks = [asyncio.create_task(run_update(i)) for i in range(num_parallel)]
            gate.set()
            results = await asyncio.gather(*tasks)

            for result in results:
                assert result == "Updated workflow status from Processed to Completed"

        async def check_updates_on_completed_workflows_fail():
            """The handler workflow already finished at this point, further updaes should just fail"""
            wf_handle = await client.start_workflow(
                UpdateWorkflowCaller.run,
                Input(
                    value=update_workflow_id,
                    task_queue=task_queue,
                    update_value="dummy, will fail anyway",
                ),
                task_queue=task_queue,
                id=f"{uuid.uuid4()}",
            )
            with pytest.raises(WorkflowFailureError):
                await wf_handle.result()

        await check_simple_update_and_links()
        await check_sequential_updates_consistent()
        await check_parallel_updates_idempotent_and_finish()
        await check_updates_on_completed_workflows_fail()


async def test_temporal_operation_update_workflow_delayed(
    client: Client, env: WorkflowEnvironment
) -> None:
    if (
        env.supports_time_skipping
    ):  # time skipping server uses different dynamic configs
        pytest.skip("Update workflow tests don't work with time-skipping server")
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)

    update_workflow_id = f"another-updatable-workflow-{uuid.uuid4()}"

    # start both caller and handler without starting worker
    wf_handle = await client.start_workflow(
        UpdateWorkflowCaller.run,
        Input(
            value=update_workflow_id,
            task_queue=task_queue,
            update_value="Completed",
        ),
        task_queue=task_queue,
        id=f"update-workflow-caller-created-{uuid.uuid4()}",
    )
    target_handle = await client.start_workflow(
        UpdatableWorkflow.run, id=update_workflow_id, task_queue=task_queue
    )

    # now, start the worker, it should process both the handler
    # and the caller and finish the enqueued update
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[TestServiceHandler()],
        workflows=[UpdatableWorkflow, UpdateWorkflowCaller],
    ):
        result = await wf_handle.result()
        assert result == "Updated workflow status from Pending to Completed"

        await assert_event_subsequence(
            wf_handle,
            [
                EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED,
                EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED,
            ],
        )

        caller_history = await wf_handle.fetch_history()
        handler_history = await target_handle.fetch_history()
        scheduled_event = next(
            e
            for e in caller_history.events
            if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
        )
        caller_request_id = (
            scheduled_event.nexus_operation_scheduled_event_attributes.request_id
        )
        assert target_handle.result_run_id is not None
        expected_forward_link = Link(
            workflow_event=Link.WorkflowEvent(
                namespace=client.namespace,
                workflow_id=update_workflow_id,
                run_id=target_handle.result_run_id,
                request_id_ref=Link.WorkflowEvent.RequestIdReference(
                    request_id=caller_request_id,
                    event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED,
                ),
            )
        )
        assert wf_handle.result_run_id is not None
        expected_backward_link = Link(
            workflow_event=Link.WorkflowEvent(
                namespace=client.namespace,
                workflow_id=wf_handle.id,
                run_id=wf_handle.result_run_id,
                event_ref=Link.WorkflowEvent.EventReference(
                    event_id=scheduled_event.event_id,
                    event_type=EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                ),
            )
        )
        caller_links = [
            link
            for e in caller_history.events
            if e.event_type == EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED
            for link in e.links
        ]
        handler_links = [
            link
            for e in handler_history.events
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
            for link in e.links
        ]
        assert expected_forward_link in caller_links
        assert expected_backward_link in handler_links


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


async def test_temporal_operation_includes_token_in_callback(
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


@workflow.defn
class UpdateWorkflowCaller:
    """Simple caller workflow that triggers a workflow update via nexus op"""

    @workflow.run
    async def run(self, input: Input) -> str:
        client = workflow.create_nexus_client(
            service=TestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        op_handle = await client.start_operation(TestService.update_op, input)
        if input.expect_sync_response:
            if op_handle.operation_token:
                raise RuntimeError("unexpected operation token on a sync operation")
        else:
            if not op_handle.operation_token:
                raise RuntimeError(
                    "unexpected empty operation token on an async operation"
                )
        return await op_handle


@workflow.defn
class UpdatableWorkflow:
    """Workflow that accepts updates and exits when it receives a specific status"""

    def __init__(self) -> None:
        self.order_status = "Pending"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.order_status == "Completed")
        # some more order processing etc

    @workflow.update
    async def do_update(self, value: str) -> str:
        status = self.order_status
        await workflow.sleep(
            timedelta(seconds=1)
        )  # small sleep to ensure updates are async and backlinks are to STARTED events
        self.order_status = value
        update_result = f"Updated workflow status from {status} to {value}"
        return update_result
