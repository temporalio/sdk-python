from datetime import timedelta
from uuid import uuid4

from strands import tool
from strands_tools import (  # pyright: ignore[reportMissingTypeStubs]
    calculator,
    current_time,
)

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.contrib.strands.workflow import activity_as_tool
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


@tool
def letter_counter(word: str, letter: str) -> int:
    return word.lower().count(letter.lower())


@activity.defn(name="current_time")
async def current_time_activity() -> str:
    return current_time.current_time()


@workflow.defn
class ToolWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
            tools=[
                calculator,
                activity_as_tool(
                    current_time_activity,
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
    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
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
        }
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ToolWorkflow],
        activities=[current_time_activity],
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
    assert get_activities(history) == [
        "invoke_model",
        "current_time",
        "invoke_model",
        # calculator (in-workflow)
        "invoke_model",
        # letter_counter (in-workflow)
        "invoke_model",
    ]

    await Replayer(
        workflows=[ToolWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
