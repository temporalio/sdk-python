from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import nexusrpc.handler
import pytest

from temporalio import workflow
from temporalio.client import Client
from tests.helpers import new_worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@workflow.defn
class NexusCallerWorkflow:
    """Workflow that calls a Nexus operation."""

    @workflow.run
    async def run(self, id: int) -> None:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service="MaxConcurrentTestService",
        )

        await nexus_client.execute_operation(
            "op",
            id,
            schedule_to_close_timeout=timedelta(seconds=60),
        )


@pytest.mark.parametrize(
    ["num_nexus_operations", "max_concurrent_nexus_tasks", "expected_num_executed"],
    [(1, 1, 1), (2, 1, 1), (43, 42, 42), (43, 44, 43)],
)
async def test_max_concurrent_nexus_tasks(
    client: Client,
    max_concurrent_nexus_tasks: int,
    num_nexus_operations: int,
    expected_num_executed: int,
):
    ids = []
    event = asyncio.Event()

    @nexusrpc.handler.service_handler
    class MaxConcurrentTestService:
        @nexusrpc.handler.sync_operation
        async def op(
            self, _ctx: nexusrpc.handler.StartOperationContext, id: int
        ) -> None:
            ids.append(id)
            await event.wait()

    async with new_worker(
        client,
        NexusCallerWorkflow,
        nexus_service_handlers=[MaxConcurrentTestService()],
        max_concurrent_nexus_tasks=max_concurrent_nexus_tasks,
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, client)

        coros = [
            client.execute_workflow(
                NexusCallerWorkflow.run,
                i,
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
            )
            for i in range(num_nexus_operations)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*coros), timeout=5)
        except asyncio.TimeoutError:
            pass
        event.set()
        assert len(set(ids)) == len(ids)
        assert len(ids) == expected_num_executed
