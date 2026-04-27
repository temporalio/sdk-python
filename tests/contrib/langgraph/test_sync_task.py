import sys
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="LangGraph Functional API requires Python >= 3.11 for async context propagation",
)
from langgraph.func import (  # pyright: ignore[reportMissingTypeStubs]
    entrypoint as lg_entrypoint,
)
from langgraph.func import task  # pyright: ignore[reportMissingTypeStubs]

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, entrypoint
from temporalio.worker import Worker


@task
def sync_task(x: int) -> int:
    return x + 1


@lg_entrypoint()
async def sync_task_entrypoint(value: int) -> dict[str, int]:
    result = await sync_task(value)
    return {"result": result}


@workflow.defn
class SyncTaskWorkflow:
    def __init__(self) -> None:
        self.app = entrypoint("sync-task")

    @workflow.run
    async def run(self, input: int) -> Any:
        return await self.app.ainvoke(input)


async def test_sync_task(client: Client):
    task_queue = f"sync-task-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SyncTaskWorkflow],
        plugins=[
            LangGraphPlugin(
                entrypoints={"sync-task": sync_task_entrypoint},
                tasks=[sync_task],
                activity_options={"sync_task": {"execute_in": "activity"}},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            SyncTaskWorkflow.run,
            41,
            id=f"test-sync-task-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"result": 42}
