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
    _temporal_mcp_client,
)
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


def _echo_client_factory() -> MCPClient:
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=sys.executable,
                args=[str(Path(__file__).parent / "echo_mcp_server.py")],
            )
        )
    )


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


@workflow.defn
class MCPReuseWorkflow:
    def __init__(self) -> None:
        echo = TemporalMCPClient(
            server="echo_cached",
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


async def test_mcp_reuses_connection(client: Client):
    """Successive MCP tool calls reuse one cached worker-side connection."""
    task_queue = "test_mcp_reuses_connection"
    # Count how often the worker opens a connection. With caching this is one
    # startup-discovery connection plus one cached call connection serving both
    # tool calls (2); reconnecting per call would make it 3.
    factory_calls = [0]

    def counting_factory() -> MCPClient:
        factory_calls[0] += 1
        return _echo_client_factory()

    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "echo", "input": {"message": "one"}},
                    {"name": "echo", "input": {"message": "two"}},
                    "Done!",
                ]
            )
        },
        mcp_clients={"echo_cached": counting_factory},
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[MCPReuseWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            MCPReuseWorkflow.run,
            "echo twice",
            id=f"test_mcp_reuses_connection_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    # The worker context has exited, so its run_context finally evicted the
    # cached connection.
    assert "echo_cached" not in _temporal_mcp_client._CONNECTIONS
    assert factory_calls[0] == 2

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "invoke_model",
        "echo_cached-call-tool",
        "invoke_model",
        "echo_cached-call-tool",
        "invoke_model",
    ]

    await Replayer(
        workflows=[MCPReuseWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
