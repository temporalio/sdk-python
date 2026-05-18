import asyncio
from datetime import timedelta
from uuid import uuid4

from strands import Agent
from strands.types.streaming import StreamEvent

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalModel
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel

MODEL = TemporalModel(
    model_factory=lambda: MockModel(["Done!"]),
    start_to_close_timeout=timedelta(seconds=15),
    streaming_topic="events",
)


@workflow.defn
class StreamingModelWorkflow:
    def __init__(self) -> None:
        self.stream = WorkflowStream()
        self.agent = Agent(model=MODEL)

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        return str(result)


async def test_model_streaming(client: Client):
    task_queue = "test_model_streaming"
    plugin = StrandsPlugin(model=MODEL)
    workflow_id = f"test_model_streaming_{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[StreamingModelWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            StreamingModelWorkflow.run,
            "Hello",
            id=workflow_id,
            task_queue=task_queue,
        )

        stream = WorkflowStreamClient.create(client, workflow_id)
        events: list[StreamEvent] = []

        async def collect() -> None:
            async for item in stream.subscribe(
                ["events"],
                from_offset=0,
                result_type=StreamEvent,
                poll_cooldown=timedelta(milliseconds=50),
            ):
                events.append(item.data)
                if len(events) >= 4:
                    break

        collect_task = asyncio.create_task(collect())
        assert await handle.result() == "Done!\n"
        await asyncio.wait_for(collect_task, timeout=10.0)

    history = await handle.fetch_history()
    assert get_activities(history) == ["invoke_model_streaming"]

    assert any("messageStart" in e for e in events)
    assert any("messageStop" in e for e in events)

    await Replayer(
        workflows=[StreamingModelWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
