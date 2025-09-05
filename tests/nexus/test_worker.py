from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Optional

import nexusrpc.handler
import pytest

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import FixedSizeSlotSupplier, WorkerTuner
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
    [(1, 1, 1), (2, 1, 1), (18, 17, 17), (18, 19, 18)],
)
async def test_max_concurrent_nexus_tasks(
    client: Client,
    max_concurrent_nexus_tasks: int,
    num_nexus_operations: int,
    expected_num_executed: int,
):
    """Test max_concurrent_nexus_tasks parameter."""
    await _test_nexus_concurrency_helper(
        client=client,
        num_nexus_operations=num_nexus_operations,
        expected_num_executed=expected_num_executed,
        max_concurrent_nexus_tasks=max_concurrent_nexus_tasks,
    )


@pytest.mark.parametrize(
    ["num_nexus_operations", "nexus_slots", "expected_num_executed"],
    [(1, 1, 1), (2, 1, 1), (43, 42, 42), (43, 44, 43)],
)
async def test_max_concurrent_nexus_tasks_with_tuner(
    client: Client,
    nexus_slots: int,
    num_nexus_operations: int,
    expected_num_executed: int,
):
    """Test nexus concurrency using a WorkerTuner."""
    tuner = WorkerTuner.create_fixed(
        workflow_slots=10,
        activity_slots=10,
        local_activity_slots=10,
        nexus_slots=nexus_slots,
    )
    await _test_nexus_concurrency_helper(
        client=client,
        num_nexus_operations=num_nexus_operations,
        expected_num_executed=expected_num_executed,
        tuner=tuner,
    )


@pytest.mark.parametrize(
    ["num_nexus_operations", "nexus_supplier", "expected_num_executed"],
    [
        (1, FixedSizeSlotSupplier(1), 1),
        (2, FixedSizeSlotSupplier(1), 1),
        (43, FixedSizeSlotSupplier(42), 42),
    ],
)
async def test_max_concurrent_nexus_tasks_with_composite_tuner(
    client: Client,
    nexus_supplier: FixedSizeSlotSupplier,
    num_nexus_operations: int,
    expected_num_executed: int,
):
    """Test nexus concurrency using a composite WorkerTuner with nexus_supplier."""
    tuner = WorkerTuner.create_composite(
        workflow_supplier=FixedSizeSlotSupplier(10),
        activity_supplier=FixedSizeSlotSupplier(10),
        local_activity_supplier=FixedSizeSlotSupplier(10),
        nexus_supplier=nexus_supplier,
    )
    await _test_nexus_concurrency_helper(
        client=client,
        num_nexus_operations=num_nexus_operations,
        expected_num_executed=expected_num_executed,
        tuner=tuner,
    )


async def _test_nexus_concurrency_helper(
    client: Client,
    num_nexus_operations: int,
    expected_num_executed: int,
    max_concurrent_nexus_tasks: Optional[int] = None,
    tuner: Optional[WorkerTuner] = None,
):
    assert (max_concurrent_nexus_tasks is None) != (tuner is None)
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
        tuner=tuner,
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, client)

        tasks = [
            asyncio.create_task(
                client.execute_workflow(
                    NexusCallerWorkflow.run,
                    i,
                    id=str(uuid.uuid4()),
                    task_queue=worker.task_queue,
                )
            )
            for i in range(num_nexus_operations)
        ]

        for _ in range(50):  # 5 seconds max
            if len(ids) >= expected_num_executed:
                break
            await asyncio.sleep(0.1)

        await asyncio.sleep(0.1)
        assert len(ids) == expected_num_executed
        assert len(set(ids)) == len(ids)

        event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
