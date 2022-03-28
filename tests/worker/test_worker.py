
from typing import Type
import uuid
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio import workflow

async def test_workflow_hello(client: Client):
    @workflow.defn
    class SayHello:
        @workflow.run
        async def run(self, name: str) -> str:
            return f"Hello, {name}!"

    async with new_worker(client, SayHello) as worker:
        result = await client.execute_workflow(SayHello.run, "Temporal", id="workflow1", task_queue=worker.task_queue)
        assert result == "Hello, Temporal!"



def new_worker(client: Client, *workflows: Type) -> Worker:
    return Worker(client, task_queue=str(uuid.uuid4()), workflows=workflows)