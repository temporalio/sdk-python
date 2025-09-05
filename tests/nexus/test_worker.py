from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import nexusrpc.handler
import pytest

from temporalio import workflow
from temporalio._asyncio_compat import Barrier
from temporalio.testing import WorkflowEnvironment
from tests.helpers import new_worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


@workflow.defn
class NexusCallerWorkflow:
    """Workflow that calls a Nexus operation."""

    @workflow.run
    async def run(self, n: int) -> None:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service="MaxConcurrentTestService",
        )

        coros: list[Any] = [
            nexus_client.execute_operation(
                "op",
                i,
                schedule_to_close_timeout=timedelta(seconds=60),
            )
            for i in range(n)
        ]
        await asyncio.gather(*coros)


@pytest.mark.parametrize(
    ["num_nexus_operations", "max_concurrent_nexus_tasks"],
    [
        (1, 1),
        (3, 3),
        (4, 3),
    ],
)
async def test_max_concurrent_nexus_tasks(
    env: WorkflowEnvironment,
    max_concurrent_nexus_tasks: int,
    num_nexus_operations: int,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with Javas test server")

    barrier = Barrier(num_nexus_operations)

    @nexusrpc.handler.service_handler
    class MaxConcurrentTestService:
        @nexusrpc.handler.sync_operation
        async def op(
            self, _ctx: nexusrpc.handler.StartOperationContext, id: int
        ) -> None:
            await barrier.wait()

    async with new_worker(
        env.client,
        NexusCallerWorkflow,
        nexus_service_handlers=[MaxConcurrentTestService()],
        max_concurrent_nexus_tasks=max_concurrent_nexus_tasks,
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, env.client)

        execute_operations_concurrently = env.client.execute_workflow(
            NexusCallerWorkflow.run,
            num_nexus_operations,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        if num_nexus_operations <= max_concurrent_nexus_tasks:
            await execute_operations_concurrently
        else:
            try:
                await asyncio.wait_for(execute_operations_concurrently, timeout=10)
            except TimeoutError:
                pass
            else:
                pytest.fail(
                    f"Expected timeout: "
                    f"max_concurrent_nexus_tasks={max_concurrent_nexus_tasks}, "
                    f"num_nexus_operations={num_nexus_operations}"
                )
