import sys
from datetime import timedelta
from uuid import uuid4

import pytest

if sys.platform == "win32":
    pytest.skip(
        "strands_tools.shell uses Unix-only pty/termios",
        allow_module_level=True,
    )

from strands import tool
from strands_tools import (  # pyright: ignore[reportMissingTypeStubs]
    calculator,
    shell,
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


@activity.defn(name="shell")
async def shell_activity(command: str) -> dict:
    return shell.shell(command=command, non_interactive=True)


@workflow.defn
class ToolWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
            tools=[
                calculator,
                activity_as_tool(
                    shell_activity,
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
                    {"name": "shell", "input": {"command": "echo hello"}},
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
        activities=[shell_activity],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            ToolWorkflow.run,
            "I have 3 requests:\n"
            "1. Run `echo hello` in a shell\n"
            "2. Calculate 3111696 / 74088\n"
            '3. Tell me how many letter R\'s are in the word "strawberry" 🍓',
            id=f"test_tool_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "invoke_model",
        "shell",
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
