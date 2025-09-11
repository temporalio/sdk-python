from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import nexusrpc.handler
import pytest

from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from tests.helpers import new_worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@workflow.defn
class NexusCallerWorkflow:
    """Workflow that calls a Nexus operation."""

    @workflow.run
    async def run(self) -> None:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service="MaxConcurrentTestService",
        )

        await nexus_client.execute_operation(
            "op", None, schedule_to_close_timeout=timedelta(seconds=60)
        )


async def test_nexus_timeout(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with Javas test server")

    @nexusrpc.handler.service_handler
    class MaxConcurrentTestService:
        @nexusrpc.handler.sync_operation
        async def op(
            self, _ctx: nexusrpc.handler.StartOperationContext, id: None
        ) -> None:
            await asyncio.Event().wait()

    async with new_worker(
        env.client,
        NexusCallerWorkflow,
        nexus_service_handlers=[MaxConcurrentTestService()],
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, env.client)

        execute_workflow = env.client.execute_workflow(
            NexusCallerWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        try:
            await asyncio.wait_for(execute_workflow, timeout=3)
        except TimeoutError:
            print("Saw expected timeout")
            pass
        else:
            pytest.fail("Expected timeout")
