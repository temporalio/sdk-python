from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
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
)
import temporalio.exceptions
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name

operation_invocation_counts = Counter[str]()


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
        self, ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise Exception

    @nexusrpc.handler.sync_operation
    def retried_due_to_retryable_application_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise ApplicationError(
            "application-error-message",
            type="application-error-type",
            non_retryable=False,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_resource_exhausted_handler_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.RESOURCE_EXHAUSTED,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_internal_handler_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.INTERNAL,
        )

    @nexusrpc.handler.sync_operation
    async def fails_due_to_workflow_already_started(
        self, ctx: nexusrpc.handler.StartOperationContext, input: ErrorTestInput
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
        await create_nexus_endpoint(input.task_queue, client)
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
        await create_nexus_endpoint(input.task_queue, client)
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
    async def op_handler_that_never_returns(
        self, ctx: StartOperationContext, input: None
    ) -> None:
        try:
            await asyncio.wait_for(ctx.task_cancellation.wait_until_cancelled(), 1)
        except asyncio.TimeoutError:
            print("timeout")
            raise ApplicationError("expected cancel", non_retryable=True)

    @sync_operation
    def op_handler_that_never_returns_but_sync(
        self, ctx: StartOperationContext, input: None
    ) -> None:
        cancelled = ctx.task_cancellation.wait_until_cancelled_sync(1)
        if not cancelled:
            raise ApplicationError("expected cancel", non_retryable=True)
        reason = ctx.task_cancellation.cancellation_reason()
        if reason != "timeout":
            raise ApplicationError("expected cancel details", non_retryable=True)


@workflow.defn
class StartTimeoutTestCallerWorkflowSync:
    @workflow.init
    def __init__(self):
        self.nexus_client = workflow.create_nexus_client(
            service=StartTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self) -> None:
        await self.nexus_client.execute_operation(
            StartTimeoutTestService.op_handler_that_never_returns_but_sync,  # type: ignore[arg-type] # mypy can't infer OutputT=None in Union type
            None,
            output_type=None,
            schedule_to_close_timeout=timedelta(seconds=0.1),
        )


@workflow.defn
class StartTimeoutTestCallerWorkflow:
    @workflow.init
    def __init__(self):
        self.nexus_client = workflow.create_nexus_client(
            service=StartTimeoutTestService,
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        )

    @workflow.run
    async def run(self) -> None:
        await self.nexus_client.execute_operation(
            StartTimeoutTestService.op_handler_that_never_returns,  # type: ignore[arg-type] # mypy can't infer OutputT=None in Union type
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
        workflows=[StartTimeoutTestCallerWorkflow, StartTimeoutTestCallerWorkflowSync],
        task_queue=task_queue,
        nexus_task_executor=concurrent.futures.ThreadPoolExecutor(),
    ):
        await create_nexus_endpoint(task_queue, client)
        try:
            await client.execute_workflow(
                StartTimeoutTestCallerWorkflow.run,
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            assert isinstance(err.__cause__.__cause__, TimeoutError)
        else:
            pytest.fail("Expected exception due to timeout of nexus start operation")

        try:
            await client.execute_workflow(
                StartTimeoutTestCallerWorkflowSync.run,
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
        except Exception as err:
            assert isinstance(err, WorkflowFailureError)
            assert isinstance(err.__cause__, NexusOperationError)
            assert isinstance(err.__cause__.__cause__, TimeoutError)
        else:
            pytest.fail("Expected exception due to timeout of nexus start operation")


# Cancellation timeout test


class OperationWithCancelMethodThatNeverReturns(OperationHandler[None, None]):
    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync("fake-token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        try:
            await asyncio.wait_for(ctx.task_cancellation.wait_until_cancelled(), 10)
        except asyncio.TimeoutError:
            raise RuntimeError("expected cancellation")


@service_handler
class CancellationTimeoutTestService:
    @nexusrpc.handler._decorators.operation_handler
    def op_with_cancel_method_that_never_returns(
        self,
    ) -> OperationHandler[None, None]:
        return OperationWithCancelMethodThatNeverReturns()


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
        # TODO(nexus-prerelease)
        op_handle = await self.nexus_client.start_operation(
            # Although the tests are making use of it, we are not exposing operation
            # factory methods to users as a way to write nexus operations, and so the
            # types on NexusClient start_operation/execute_operation do not currently
            # permit it.
            CancellationTimeoutTestService.op_with_cancel_method_that_never_returns,  # type: ignore
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
        await create_nexus_endpoint(task_queue, client)
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
            pytest.fail("Expected exception due to timeout of nexus cancel operation")
