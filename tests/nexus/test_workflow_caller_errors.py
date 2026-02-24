from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from logging import getLogger

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    operation_handler,
    service_handler,
    sync_operation,
)

from temporalio import nexus, workflow
from temporalio.client import (
    Client,
    WorkflowFailureError,
)
from temporalio.exceptions import (
    ApplicationError,
    NexusOperationError,
    TimeoutError,
    TimeoutType,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import LogCapturer, assert_eq_eventually
from tests.helpers.nexus import make_nexus_endpoint_name

operation_invocation_counts = Counter[str]()

logger = getLogger(__name__)


@dataclass
class ErrorTestInput:
    service_name: str
    operation_name: str
    task_queue: str
    id: str


@workflow.defn
class NonTerminatingWorkflow:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Event().wait()


@nexusrpc.handler.service_handler
class ErrorTestService:
    @nexusrpc.handler.sync_operation
    def retried_due_to_exception(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise Exception

    @nexusrpc.handler.sync_operation
    def retried_due_to_retryable_application_error(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise ApplicationError(
            "application-error-message",
            type="application-error-type",
            non_retryable=False,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_resource_exhausted_handler_error(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.RESOURCE_EXHAUSTED,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_internal_handler_error(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.INTERNAL,
        )

    @nexusrpc.handler.sync_operation
    async def fails_due_to_workflow_already_started(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        for _ in range(2):
            await nexus.client().start_workflow(
                NonTerminatingWorkflow.run,
                id="second-start-request-will-fail",
                task_queue=nexus.info().task_queue,
            )


@workflow.defn(sandboxed=False)
class CallerWorkflow:
    @workflow.run
    async def run(self, input: ErrorTestInput) -> None:
        nexus_client = workflow.create_nexus_client(
            service=input.service_name,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        await nexus_client.execute_operation(input.operation_name, input)


@pytest.mark.parametrize(
    "operation_name",
    [
        "retried_due_to_exception",
        "retried_due_to_retryable_application_error",
        "retried_due_to_resource_exhausted_handler_error",
        "retried_due_to_internal_handler_error",
    ],
)
async def test_nexus_operation_is_retried(
    client: Client, env: WorkflowEnvironment, operation_name: str
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    input = ErrorTestInput(
        service_name="ErrorTestService",
        operation_name=operation_name,
        task_queue=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
    )
    async with Worker(
        client,
        nexus_service_handlers=[ErrorTestService()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1),
        workflows=[CallerWorkflow],
        task_queue=input.task_queue,
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(input.task_queue), input.task_queue
        )
        asyncio.create_task(
            client.execute_workflow(
                CallerWorkflow.run,
                input,
                id=str(uuid.uuid4()),
                task_queue=input.task_queue,
            )
        )

        async def times_called() -> int:
            return operation_invocation_counts[input.id]

        await assert_eq_eventually(2, times_called)


@pytest.mark.parametrize(
    ["operation_name", "handler_error_type", "handler_error_message"],
    [
        (
            "fails_due_to_nonexistent_operation",
            nexusrpc.HandlerErrorType.NOT_FOUND,
            "has no operation",
        ),
        (
            "fails_due_to_nonexistent_service",
            nexusrpc.HandlerErrorType.NOT_FOUND,
            "No handler for service",
        ),
        (
            "fails_due_to_workflow_already_started",
            nexusrpc.HandlerErrorType.INTERNAL,
            "already started",
        ),
    ],
)
async def test_nexus_operation_fails_without_retry_as_handler_error(
    client: Client,
    env: WorkflowEnvironment,
    operation_name: str,
    handler_error_type: nexusrpc.HandlerErrorType,
    handler_error_message: str,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    input = ErrorTestInput(
        service_name=(
            "ErrorTestService"
            if operation_name != "fails_due_to_nonexistent_service"
            else "NonExistentService"
        ),
        operation_name=operation_name,
        task_queue=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
    )
    async with Worker(
        client,
        nexus_service_handlers=[ErrorTestService()],
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1),
        workflows=[CallerWorkflow],
        task_queue=input.task_queue,
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(input.task_queue), input.task_queue
        )
        try:
            await client.execute_workflow(
                CallerWorkflow.run,
                input,
                id=str(uuid.uuid4()),
                task_queue=input.task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            handler_error = err.__cause__.__cause__
            assert isinstance(handler_error, nexusrpc.HandlerError)
            assert not handler_error.retryable
            assert handler_error.type == handler_error_type
            assert handler_error_message in str(handler_error)
        else:
            pytest.fail("Unreachable")


# Start timeout test
@service_handler
class StartTimeoutTestService:
    @sync_operation
    async def expect_timeout_cancellation_async(
        self, ctx: StartOperationContext, _input: None
    ) -> None:
        try:
            await asyncio.wait_for(ctx.task_cancellation.wait_until_cancelled(), 1)
        except asyncio.TimeoutError:
            raise ApplicationError("expected cancel", non_retryable=True)

    @sync_operation
    def expect_timeout_cancellation_sync(
        self, ctx: StartOperationContext, _input: None
    ) -> None:
        ctx.task_cancellation.wait_until_cancelled_sync(5)


@workflow.defn
class StartTimeoutTestCallerWorkflow:
    @workflow.init
    def __init__(self, operation: str):
        self.nexus_client = workflow.create_nexus_client(
            service=StartTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self, operation: str) -> None:
        await self.nexus_client.execute_operation(
            operation,
            None,
            output_type=None,
            schedule_to_close_timeout=timedelta(seconds=0.1),
        )


async def test_error_raised_by_timeout_of_nexus_start_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[StartTimeoutTestService()],
        workflows=[StartTimeoutTestCallerWorkflow],
        task_queue=task_queue,
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        try:
            await client.execute_workflow(
                StartTimeoutTestCallerWorkflow.run,
                "expect_timeout_cancellation_async",
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            assert isinstance(err.__cause__.__cause__, TimeoutError)
        else:
            pytest.fail("Expected exception due to timeout of nexus start operation")

        with LogCapturer().logs_captured(logger) as capturer:
            try:
                await client.execute_workflow(
                    StartTimeoutTestCallerWorkflow.run,
                    "expect_timeout_cancellation_sync",
                    id=str(uuid.uuid4()),
                    task_queue=task_queue,
                )
            except Exception as err:
                assert isinstance(err, WorkflowFailureError)
                assert isinstance(err.__cause__, NexusOperationError)
                assert isinstance(err.__cause__.__cause__, TimeoutError)
            else:
                pytest.fail(
                    "Expected exception due to timeout of nexus start operation"
                )
            assert capturer.find_log("unexpected cancellation reason") is None


# Schedule to start timeout test
@service_handler
class ScheduleToStartTimeoutTestService:
    @sync_operation
    async def expect_schedule_to_start_timeout(
        self, ctx: StartOperationContext, _input: None
    ) -> None:
        try:
            await asyncio.wait_for(ctx.task_cancellation.wait_until_cancelled(), 1)
        except asyncio.TimeoutError:
            raise ApplicationError("expected cancel", non_retryable=True)


@workflow.defn
class ScheduleToStartTimeoutTestCallerWorkflow:
    @workflow.init
    def __init__(self):
        self.nexus_client = workflow.create_nexus_client(
            service=ScheduleToStartTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self) -> None:
        await self.nexus_client.execute_operation(
            ScheduleToStartTimeoutTestService.expect_schedule_to_start_timeout,
            None,
            output_type=None,
            schedule_to_start_timeout=timedelta(seconds=0.1),
        )


async def test_error_raised_by_schedule_to_start_timeout_of_nexus_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ScheduleToStartTimeoutTestService()],
        workflows=[ScheduleToStartTimeoutTestCallerWorkflow],
        task_queue=task_queue,
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        try:
            await client.execute_workflow(
                ScheduleToStartTimeoutTestCallerWorkflow.run,
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            assert isinstance(err.__cause__.__cause__, TimeoutError)
            timeout_err = err.__cause__.__cause__
            assert timeout_err.type == TimeoutType.SCHEDULE_TO_START
        else:
            pytest.fail(
                "Expected exception due to schedule to start timeout of nexus operation"
            )


# Start to close timeout test


class OperationThatExpectsStartToCloseTimeoutAsync(OperationHandler[None, None]):
    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync("fake-token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        pass


@service_handler
class StartToCloseTimeoutTestService:
    @operation_handler
    def expect_start_to_close_timeout(self) -> OperationHandler[None, None]:
        return OperationThatExpectsStartToCloseTimeoutAsync()


@workflow.defn
class StartToCloseTimeoutTestCallerWorkflow:
    @workflow.init
    def __init__(
        self,
    ):
        self.nexus_client = workflow.create_nexus_client(
            service=StartToCloseTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self) -> None:
        op_handle = await self.nexus_client.start_operation(
            StartToCloseTimeoutTestService.expect_start_to_close_timeout,
            None,
            start_to_close_timeout=timedelta(seconds=0.1),
        )
        await op_handle


async def test_error_raised_by_start_to_close_timeout_of_nexus_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[StartToCloseTimeoutTestService()],
        workflows=[StartToCloseTimeoutTestCallerWorkflow],
        task_queue=task_queue,
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        try:
            await client.execute_workflow(
                StartToCloseTimeoutTestCallerWorkflow.run,
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            timeout_err = err.__cause__.__cause__
            assert isinstance(timeout_err, TimeoutError)
            assert timeout_err.type == TimeoutType.START_TO_CLOSE
        else:
            pytest.fail(
                "Expected exception due to start to close timeout of nexus operation"
            )


# Cancellation timeout test


class OperationWithCancelMethodThatExpectsCancel(OperationHandler[None, None]):
    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync("fake-token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        try:
            await asyncio.wait_for(ctx.task_cancellation.wait_until_cancelled(), 1)
        except asyncio.TimeoutError:
            logger.error("expected cancellation")
            raise ApplicationError("expected cancellation", non_retryable=True)


@service_handler
class CancellationTimeoutTestService:
    @operation_handler
    def op_with_cancel_method_that_expects_cancel(
        self,
    ) -> OperationHandler[None, None]:
        return OperationWithCancelMethodThatExpectsCancel()


@workflow.defn
class CancellationTimeoutTestCallerWorkflow:
    @workflow.init
    def __init__(self):
        self.nexus_client = workflow.create_nexus_client(
            service=CancellationTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self) -> None:
        op_handle = await self.nexus_client.start_operation(
            CancellationTimeoutTestService.op_with_cancel_method_that_expects_cancel,
            None,
            schedule_to_close_timeout=timedelta(seconds=0.1),
        )
        op_handle.cancel()
        await op_handle


async def test_error_raised_by_timeout_of_nexus_cancel_operation(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[CancellationTimeoutTestService()],
        workflows=[CancellationTimeoutTestCallerWorkflow],
        task_queue=task_queue,
    ):
        with LogCapturer().logs_captured(logger) as capturer:
            await env.create_nexus_endpoint(
                make_nexus_endpoint_name(task_queue), task_queue
            )
            try:
                await client.execute_workflow(
                    CancellationTimeoutTestCallerWorkflow.run,
                    id=str(uuid.uuid4()),
                    task_queue=task_queue,
                )
            except Exception as err:
                assert isinstance(err, WorkflowFailureError)
                assert isinstance(err.__cause__, NexusOperationError)
                assert isinstance(err.__cause__.__cause__, TimeoutError)
            else:
                pytest.fail(
                    "Expected exception due to timeout of nexus cancel operation"
                )

            assert capturer.find_log("expected cancellation") is None
