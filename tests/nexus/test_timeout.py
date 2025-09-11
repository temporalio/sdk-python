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


@nexusrpc.handler.service_handler
class NexusService:
    @nexusrpc.handler.sync_operation
    async def op_that_never_returns(
        self, _ctx: nexusrpc.handler.StartOperationContext, id: None
    ) -> None:
        await asyncio.Event().wait()


@workflow.defn
class NexusCallerWorkflow:
    @workflow.run
    async def run(self) -> None:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=NexusService,
        )
        await nexus_client.execute_operation(
            NexusService.op_that_never_returns,
            None,
            schedule_to_close_timeout=timedelta(seconds=10),
        )


async def test_nexus_timeout(env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with Javas test server")

    async with new_worker(
        env.client,
        NexusCallerWorkflow,
        nexus_service_handlers=[NexusService()],
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
