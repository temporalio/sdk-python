from datetime import timedelta
from uuid import uuid4

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


@workflow.defn
class ModelWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
        )

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        return str(result)


async def test_model(client: Client):
    task_queue = "test_model"
    plugin = StrandsPlugin(models={"mock": lambda: MockModel(["Done!"])})

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ModelWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            ModelWorkflow.run,
            "Hello",
            id=f"test_model_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    assert get_activities(history) == ["invoke_model"]

    await Replayer(
        workflows=[ModelWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
