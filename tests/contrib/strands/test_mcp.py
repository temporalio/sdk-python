import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp.mcp_client import MCPClient

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import (
    StrandsPlugin,
    TemporalAgent,
    TemporalMCPClient,
)
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


@workflow.defn
class MCPWorkflow:
    def __init__(self) -> None:
        echo = TemporalMCPClient(
            server="echo",
            start_to_close_timeout=timedelta(seconds=30),
        )
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=30),
            tools=[echo],
        )

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        return str(result)


async def test_mcp(client: Client):
    task_queue = "test_mcp"
    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "echo", "input": {"message": "hello"}},
                    "Done!",
                ]
            )
        },
        mcp_clients={
            "echo": lambda: MCPClient(
                lambda: stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=[str(Path(__file__).parent / "echo_mcp_server.py")],
                    )
                )
            ),
        },
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[MCPWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            MCPWorkflow.run,
            "echo hello",
            id=f"test_mcp_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "invoke_model",
        "echo-call-tool",
        "invoke_model",
    ]

    await Replayer(
        workflows=[MCPWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
