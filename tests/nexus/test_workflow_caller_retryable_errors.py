from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from collections import Counter
from dataclasses import dataclass

import nexusrpc
import nexusrpc.handler
import pytest

from temporalio import workflow
from temporalio.client import (
    Client,
)
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name

operation_invocation_counts = Counter()


@dataclass
class RetryTestInput:
    operation_name: str
    task_queue: str
    id: str


@nexusrpc.handler.service_handler
class RetryTestService:
    @nexusrpc.handler.sync_operation
    def retried_due_to_exception(
        self, ctx: nexusrpc.handler.StartOperationContext, input: RetryTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise Exception

    @nexusrpc.handler.sync_operation
    def retried_due_to_retryable_application_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: RetryTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise ApplicationError(
            "application-error-message",
            type="application-error-type",
            non_retryable=False,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_resource_exhausted_handler_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: RetryTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.RESOURCE_EXHAUSTED,
        )

    @nexusrpc.handler.sync_operation
    def retried_due_to_internal_handler_error(
        self, ctx: nexusrpc.handler.StartOperationContext, input: RetryTestInput
    ) -> None:
        operation_invocation_counts[input.id] += 1
        raise nexusrpc.HandlerError(
            "handler-error-message",
            type=nexusrpc.HandlerErrorType.INTERNAL,
        )


@workflow.defn(sandboxed=False)
class CallerWorkflow:
    @workflow.run
    async def run(self, input: RetryTestInput) -> None:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(input.task_queue),
            service=RetryTestService,
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
    input = RetryTestInput(
        operation_name=operation_name,
        task_queue=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
    )
    async with Worker(
        client,
        nexus_service_handlers=[RetryTestService()],
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
