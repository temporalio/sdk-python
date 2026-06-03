import asyncio
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
            cache_tools=True,
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
        "echo-list-tools",
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
            cache_tools=True,
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
    # Count how often the worker opens a connection. One lazily-opened
    # connection serves the list-tools discovery and both tool calls (1);
    # reconnecting per call would make it more.
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
    assert factory_calls[0] == 1

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "echo_cached-list-tools",
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


@workflow.defn
class MCPIdleWorkflow:
    def __init__(self) -> None:
        echo = TemporalMCPClient(
            server="echo_idle",
            cache_tools=True,
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


async def test_mcp_connection_idle_timeout(client: Client):
    """A short idle timeout evicts the cached connection while the worker runs."""
    task_queue = "test_mcp_connection_idle_timeout"
    factory_calls = [0]

    def counting_factory() -> MCPClient:
        factory_calls[0] += 1
        return _echo_client_factory()

    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "echo", "input": {"message": "hello"}},
                    "Done!",
                ]
            )
        },
        mcp_clients={"echo_idle": counting_factory},
        # Short window so the cached call connection is evicted mid-run rather
        # than only at worker shutdown. The idle timer only arms once the call
        # releases the connection, so this can't tear it down mid-call.
        mcp_connection_idle_timeout=timedelta(milliseconds=100),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[MCPIdleWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            MCPIdleWorkflow.run,
            "echo hello",
            id=f"test_mcp_connection_idle_timeout_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

        # A connection was opened lazily (on the first list-tools/call-tool).
        # How many times depends on whether the short idle timer fires between
        # activities, so this only asserts that at least one was opened; the
        # eviction-while-alive behavior is what the polling loop below checks.
        assert factory_calls[0] >= 1

        # Still inside the worker context: the short idle timer evicts the
        # cached call connection on its own. Asserting eviction here -- with the
        # worker alive -- proves it came from the idle timer, not shutdown.
        for _ in range(100):
            if "echo_idle" not in _temporal_mcp_client._CONNECTIONS:
                break
            await asyncio.sleep(0.1)
        assert "echo_idle" not in _temporal_mcp_client._CONNECTIONS


@workflow.defn
class MCPNoCacheWorkflow:
    def __init__(self) -> None:
        echo = TemporalMCPClient(
            server="echo_nocache",
            cache_tools=False,
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


async def test_mcp_lists_tools_each_turn_when_uncached(client: Client):
    """With cache_tools=False the tool list is re-fetched on every model call."""
    task_queue = "test_mcp_lists_tools_each_turn_when_uncached"
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
        mcp_clients={"echo_nocache": _echo_client_factory},
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[MCPNoCacheWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            MCPNoCacheWorkflow.run,
            "echo twice",
            id=f"test_mcp_lists_tools_each_turn_when_uncached_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    activities = get_activities(history)
    # One list-tools per model call -- the tools are re-listed every turn rather
    # than once for the workflow.
    assert activities.count("echo_nocache-list-tools") == activities.count(
        "invoke_model"
    )
    assert activities.count("echo_nocache-list-tools") == 3

    await Replayer(
        workflows=[MCPNoCacheWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
