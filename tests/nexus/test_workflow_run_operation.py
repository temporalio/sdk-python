import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import nexusrpc
import pytest
from nexusrpc import Operation, service
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
)
from nexusrpc.handler._decorators import operation_handler

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.nexus._operation_handlers import WorkflowRunOperationHandler
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class Input:
    value: str


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


class MyOperation(WorkflowRunOperationHandler):
    # TODO(nexus-preview) WorkflowRunOperationHandler is not currently implemented to
    # support subclassing as this test does.
    def __init__(self):  # type: ignore[reportMissingSuperCall]
        pass

    async def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        tctx = WorkflowRunOperationContext._from_start_operation_context(ctx)
        handle = await tctx.start_workflow(
            EchoWorkflow.run,
            input.value,
            id=str(uuid.uuid4()),
        )
        return StartOperationResultAsync(handle.to_token())


@service_handler
class SubclassingHappyPath:
    @operation_handler
    def op(self) -> OperationHandler[Input, str]:
        return MyOperation()


@service
class RequestDeadlineService:
    op: Operation[Input, str]


@service_handler(service=RequestDeadlineService)
class RequestDeadlineHandler:
    def __init__(self) -> None:
        self.start_deadlines_received: list[datetime | None] = []

    @workflow_run_operation
    async def op(
        self, ctx: WorkflowRunOperationContext, input: Input
    ) -> nexus.WorkflowHandle[str]:
        self.start_deadlines_received.append(ctx.request_deadline)
        return await ctx.start_workflow(
            EchoWorkflow.run,
            input.value,
            id=str(uuid.uuid4()),
        )


@workflow.defn
class RequestDeadlineWorkflow:
    @workflow.run
    async def run(self, input: Input, task_queue: str) -> str:
        client = workflow.create_nexus_client(
            service=RequestDeadlineService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await client.execute_operation(
            RequestDeadlineService.op,
            input,
        )


@service
class Service:
    op: Operation[Input, str]


@service_handler(service=Service)
class SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition:
    # Despite the lack of annotations on the service impl, the service definition
    # provides the type needed to deserialize the input into Input so that input.value
    # succeeds.
    @operation_handler
    def op(self) -> OperationHandler:
        return MyOperation()


@workflow.defn
class CallerWorkflow:
    @workflow.run
    async def run(self, input: Input, service_name: str, task_queue: str) -> str:
        client = workflow.create_nexus_client(
            service=service_name,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await client.execute_operation("op", input, output_type=str)


@pytest.mark.parametrize(
    "service_handler_cls",
    [
        SubclassingHappyPath,
        SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition,
    ],
)
async def test_workflow_run_operation(
    client: Client,
    env: WorkflowEnvironment,
    service_handler_cls: type[Any],
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    assert (service_defn := nexusrpc.get_service_definition(service_handler_cls))
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler_cls()],
        workflows=[CallerWorkflow, EchoWorkflow],
    ):
        result = await client.execute_workflow(
            CallerWorkflow.run,
            args=[Input(value="test"), service_defn.name, task_queue],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == "test"


async def test_request_deadline_is_accessible_in_workflow_run_operation(
    client: Client,
    env: WorkflowEnvironment,
):
    """Test that request_deadline is accessible in WorkflowRunOperationContext."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    await env.create_nexus_endpoint(endpoint_name, task_queue)
    service_handler = RequestDeadlineHandler()
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
        workflows=[RequestDeadlineWorkflow, EchoWorkflow],
    ):
        await client.execute_workflow(
            RequestDeadlineWorkflow.run,
            args=[Input(value="test"), task_queue],
            task_queue=task_queue,
            id=str(uuid.uuid4()),
        )

        assert len(service_handler.start_deadlines_received) == 1
        deadline = service_handler.start_deadlines_received[0]
        assert (
            deadline is not None
        ), "request_deadline should be set in WorkflowRunOperationContext"
        assert deadline.tzinfo is timezone.utc, "request_deadline should be in utc"
