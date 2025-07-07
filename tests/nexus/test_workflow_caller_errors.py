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
    FetchOperationInfoContext,
    FetchOperationResultContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
    sync_operation,
)

from temporalio import workflow
from temporalio.client import (
    Client,
    WorkflowFailureError,
)
from temporalio.exceptions import (
    ApplicationError,
    NexusOperationError,
    TimeoutError,
)
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name

operation_invocation_counts = Counter()


@dataclass
class ErrorTestInput:
    service_name: str
    operation_name: str
    task_queue: str
    id: str


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
async def test_nexus_operation_is_retried(client: Client, operation_name: str):
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
    ],
)
async def test_nexus_operation_fails_without_retry_as_handler_error(
    client: Client,
    operation_name: str,
    handler_error_type: nexusrpc.HandlerErrorType,
    handler_error_message: str,
):
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
        await asyncio.Future()


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
            StartTimeoutTestService.op_handler_that_never_returns,
            None,
            schedule_to_close_timeout=timedelta(seconds=0.1),
        )


async def test_error_raised_by_timeout_of_nexus_start_operation(client: Client):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[StartTimeoutTestService()],
        workflows=[StartTimeoutTestCallerWorkflow],
        task_queue=task_queue,
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


# Cancellation timeout test


class OperationWithCancelMethodThatNeverReturns(OperationHandler[None, None]):
    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync("fake-token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        await asyncio.Future()

    async def fetch_info(
        self, ctx: FetchOperationInfoContext, token: str
    ) -> nexusrpc.OperationInfo:
        raise NotImplementedError("Not implemented")

    async def fetch_result(self, ctx: FetchOperationResultContext, token: str) -> None:
        raise NotImplementedError("Not implemented")


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


async def test_error_raised_by_timeout_of_nexus_cancel_operation(client: Client):
    pytest.skip("TODO(nexus-prerelease): finish writing this test")
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
