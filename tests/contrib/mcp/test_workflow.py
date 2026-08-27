import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from temporalio import workflow
from temporalio.contrib.mcp import TemporalMCPClient

with workflow.unsafe.imports_passed_through():
    import uvicorn
    from mcp import Client, StdioServerParameters, stdio_client
    from mcp.server.mcpserver import MCPServer
    from mcp.types import TextContent, TextResourceContents

    from temporalio.client import Client as TemporalClient
    from temporalio.contrib.mcp import MCPPlugin
    from temporalio.worker import Replayer
    from tests.helpers import find_free_port, new_worker


def rich_server() -> MCPServer[Any]:
    server = MCPServer("rich")

    @server.tool()
    def echo(value: str) -> str:  # type: ignore[reportUnusedFunction]
        return value

    @server.prompt()
    def greeting(name: str) -> str:  # type: ignore[reportUnusedFunction]
        return f"Hello {name}"

    @server.resource("test://static", name="static")
    def static_resource() -> str:  # type: ignore[reportUnusedFunction]
        return "resource contents"

    @server.resource("test://items/{item}", name="item")
    def item_resource(item: str) -> str:  # type: ignore[reportUnusedFunction]
        return item

    return server


@workflow.defn
class NativeMCPWorkflow:
    @workflow.run
    async def run(self) -> tuple[str, str, str, str, str, str, str]:
        client = TemporalMCPClient("rich")
        tools = await client.list_tools()
        # The second call is served from replay-safe workflow state.
        assert await client.list_tools() is tools
        tool_result = await client.call_tool("echo", {"value": "hello"})
        prompts = await client.list_prompts()
        prompt = await client.get_prompt("greeting", {"name": "Temporal"})
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        resource = await client.read_resource("test://static")
        return (
            tools.tools[0].name,
            cast(TextContent, tool_result.content[0]).text,
            prompts.prompts[0].name,
            cast(TextContent, prompt.messages[0].content).text,
            resources.resources[0].name,
            templates.resource_templates[0].name,
            cast(TextResourceContents, resource.contents[0]).text,
        )


@workflow.defn
class TransportMCPWorkflow:
    @workflow.run
    async def run(self, server: str) -> str:
        client = TemporalMCPClient(server)
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["echo"]
        result = await client.call_tool("echo", {"value": "transport"})
        return cast(TextContent, result.content[0]).text


async def activity_names(handle: Any) -> list[str]:
    names: list[str] = []
    async for event in handle.fetch_history_events():
        if event.HasField("activity_task_scheduled_event_attributes"):
            names.append(
                event.activity_task_scheduled_event_attributes.activity_type.name
            )
    return names


async def test_native_workflow_operations_and_replay(client: TemporalClient) -> None:
    server = rich_server()
    plugin = MCPPlugin({"rich": lambda: Client(server)})
    async with new_worker(client, NativeMCPWorkflow, plugins=[plugin]) as worker:
        handle = await client.start_workflow(
            NativeMCPWorkflow.run,
            id=f"native-mcp-{uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == (
            "echo",
            "hello",
            "greeting",
            "Hello Temporal",
            "static",
            "item",
            "resource contents",
        )
        names = await activity_names(handle)
        history = await handle.fetch_history()

    assert names == [
        "temporalio.contrib.mcp.rich.list-tools",
        "temporalio.contrib.mcp.rich.call-tool",
        "temporalio.contrib.mcp.rich.list-prompts",
        "temporalio.contrib.mcp.rich.get-prompt",
        "temporalio.contrib.mcp.rich.list-resources",
        "temporalio.contrib.mcp.rich.list-resource-templates",
        "temporalio.contrib.mcp.rich.read-resource",
    ]
    await Replayer(workflows=[NativeMCPWorkflow], plugins=[plugin]).replay_workflow(
        history
    )


async def run_transport_workflow(
    client: TemporalClient,
    name: str,
    factory: Callable[[], Client],
) -> None:
    async with new_worker(
        client,
        TransportMCPWorkflow,
        plugins=[MCPPlugin({name: factory}, connection_idle_timeout=None)],
    ) as worker:
        result = await client.execute_workflow(
            TransportMCPWorkflow.run,
            name,
            id=f"mcp-{name}-{uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == "transport"


async def test_stdio_transport(client: TemporalClient) -> None:
    server_path = Path(__file__).parent / "echo_mcp_server.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
    )
    await run_transport_workflow(
        client,
        "stdio",
        lambda: Client(stdio_client(parameters)),
    )


@asynccontextmanager
async def streamable_http_server() -> AsyncIterator[str]:
    server = MCPServer("http-echo")

    @server.tool()
    def echo(value: str) -> str:  # type: ignore[reportUnusedFunction]
        return value

    port = find_free_port()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host="127.0.0.1",
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(uvicorn_server.serve())
    try:
        for _ in range(500):
            if uvicorn_server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("Streamable HTTP MCP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)


async def test_streamable_http_transport(client: TemporalClient) -> None:
    async with streamable_http_server() as url:
        await run_transport_workflow(client, "http", lambda: Client(url))
