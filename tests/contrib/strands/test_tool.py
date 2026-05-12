from datetime import timedelta
from uuid import uuid4

from strands import Agent, tool
from strands_tools import calculator, current_time

from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.strands import StrandsPlugin, as_activity
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.mock_model import MockModel


@tool
def letter_counter(word: str, letter: str) -> int:
    return word.lower().count(letter.lower())


@workflow.defn
class ToolWorkflow:
    def __init__(self) -> None:
        model = MockModel(
            [
                {"name": "current_time", "input": {}},
                {
                    "name": "calculator",
                    "input": {"expression": "3111696 / 74088"},
                },
                {
                    "name": "letter_counter",
                    "input": {"word": "strawberry", "letter": "R"},
                },
                "Done!",
            ]
        )
        self.agent = Agent(
            model=model,
            tools=[
                calculator,
                as_activity(
                    current_time,
                    start_to_close_timeout=timedelta(seconds=15),
                ),
                letter_counter,
            ],
        )

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        return str(result)


async def test_tool(client: Client):
    task_queue = "test_tool"
    plugin = StrandsPlugin(activity_tools=[current_time])

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ToolWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            ToolWorkflow.run,
            "I have 4 requests:\n"
            "1. What is the time right now?\n"
            "2. Calculate 3111696 / 74088\n"
            '3. Tell me how many letter R\'s are in the word "strawberry" 🍓',
            id=f"test_tool_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    assert get_activities(history) == ["current_time"]

    await Replayer(
        workflows=[ToolWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)


def get_activities(history: WorkflowHistory) -> list[str]:
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
