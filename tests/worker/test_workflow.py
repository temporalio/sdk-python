import uuid
from typing import Type

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_workflow_hello(client: Client):
    async with new_worker(client, HelloWorkflow) as worker:
        result = await client.execute_workflow(
            HelloWorkflow.run, "Temporal", id="workflow1", task_queue=worker.task_queue
        )
        assert result == "Hello, Temporal!"


def new_worker(client: Client, *workflows: Type) -> Worker:
    return Worker(client, task_queue=str(uuid.uuid4()), workflows=workflows)
