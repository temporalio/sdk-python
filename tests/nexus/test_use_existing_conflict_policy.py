from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from nexusrpc.handler import service_handler

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name


@dataclass
class OpInput:
    workflow_id: str
    conflict_policy: WorkflowIDConflictPolicy


@workflow.defn
class HandlerWorkflow:
    def __init__(self) -> None:
        self.result: str | None = None

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.result is not None)
        assert self.result
        return self.result

    @workflow.signal
    def complete(self, result: str) -> None:
        self.result = result


@service_handler
class NexusService:
    @nexus.workflow_run_operation
    async def workflow_backed_operation(
        self, ctx: nexus.WorkflowRunOperationContext, input: OpInput
    ) -> nexus.WorkflowHandle[str]:
        return await ctx.start_workflow(
            HandlerWorkflow.run,
            id=input.workflow_id,
            id_conflict_policy=input.conflict_policy,
        )


@dataclass
class CallerWorkflowInput:
    workflow_id: str
    task_queue: str
    num_operations: int


@workflow.defn
class CallerWorkflow:
    def __init__(self) -> None:
        self._nexus_operations_have_started = asyncio.Event()

    @workflow.run
    async def run(self, input: CallerWorkflowInput) -> list[str]:
        nexus_client = workflow.create_nexus_client(
            service=NexusService, endpoint=make_nexus_endpoint_name(input.task_queue)
        )

        op_input = OpInput(
            workflow_id=input.workflow_id,
            conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

        handles = []
        for _ in range(input.num_operations):
            handles.append(
                await nexus_client.start_operation(
                    NexusService.workflow_backed_operation, op_input
                )
            )
        self._nexus_operations_have_started.set()
        return await asyncio.gather(*handles)

    @workflow.update
    async def nexus_operations_have_started(self) -> None:
        await self._nexus_operations_have_started.wait()


async def test_multiple_operation_invocations_can_connect_to_same_handler_workflow(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())

    async with Worker(
        client,
        nexus_service_handlers=[NexusService()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        caller_handle = await client.start_workflow(
            CallerWorkflow.run,
            args=[
                CallerWorkflowInput(
                    workflow_id=workflow_id,
                    task_queue=task_queue,
                    num_operations=5,
                )
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        await caller_handle.execute_update(CallerWorkflow.nexus_operations_have_started)
        await client.get_workflow_handle(workflow_id).signal(
            HandlerWorkflow.complete, "test-result"
        )
        assert await caller_handle.result() == ["test-result"] * 5
