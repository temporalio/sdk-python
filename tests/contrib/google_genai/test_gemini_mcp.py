"""MCP integration tests for the Google Gemini SDK Temporal integration.

Covers the client-side ``McpClientSession`` path (Gemini Developer API) routed
through ``TemporalMcpClientSession``:
- tool discovery + call through a real stdio MCP server on the worker
- worker-side connection pooling and idle eviction
- ``cache_tools`` listing frequency
- full parameter-schema propagation to the model (the MCP wire-format check)
- replay determinism and exact activity-scheduling counts

Plus the server-side pass-through paths that need no shim code:
- Vertex AI ``Tool(mcp_servers=[McpServer(...)])`` config serialization
- Interactions API ``MCPServerToolCallStep`` / ``MCPServerToolResultStep`` rehydration
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from google.genai import types
from google.genai._interactions._models import construct_type
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.google_genai import (
    GoogleGenAIPlugin,
    TemporalAsyncClient,
    TemporalMcpClientSession,
)
from temporalio.worker import Replayer
from temporalio.workflow import ActivityConfig
from tests.contrib.google_genai.test_gemini import (
    GeminiApiCallTracker,
    make_function_call_response,
    make_text_response,
)
from tests.helpers import new_worker

_ECHO_SERVER = str(Path(__file__).parent / "echo_mcp_server.py")


@asynccontextmanager
async def _echo_session() -> AsyncIterator[ClientSession]:
    """Yield a connected, initialized session to the stdio echo MCP server."""
    params = StdioServerParameters(command=sys.executable, args=[_ECHO_SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


class _CountingFactory:
    """Wraps the echo factory to count how often a connection is opened."""

    def __init__(self) -> None:
        self.opens = 0

    def __call__(self) -> AbstractAsyncContextManager[ClientSession]:
        self.opens += 1
        return _echo_session()


def _apply_mcp_plugin(
    client: Client,
    mock_responses: list[str],
    mcp_servers: dict,
    mcp_connection_idle_timeout: timedelta | None = None,
) -> tuple[Client, GeminiApiCallTracker]:
    """Build a plugin whose API activities are faked but MCP activities are real.

    Monkeypatches ``GeminiApiCaller.activities`` (so canned generate_content
    responses drive the AFC loop) while leaving the plugin's MCP activities —
    built from ``mcp_servers`` — to hit the real stdio echo server.
    """
    from temporalio.contrib.google_genai._gemini_activity import GeminiApiCaller

    tracker = GeminiApiCallTracker(mock_responses)
    original = GeminiApiCaller.activities
    GeminiApiCaller.activities = lambda self: [  # type: ignore[method-assign]
        tracker.gemini_api_client_async_request,
        tracker.gemini_api_client_async_request_streamed,
    ]
    try:
        from google.genai import Client as GeminiClient

        plugin = GoogleGenAIPlugin(
            GeminiClient(api_key="fake-test-key"),
            mcp_servers=mcp_servers,
            mcp_connection_idle_timeout=mcp_connection_idle_timeout,
        )
    finally:
        GeminiApiCaller.activities = original  # type: ignore[method-assign]

    config = client.config()
    config["plugins"] = [plugin]
    return Client(**config), tracker


def _replay_plugin(mcp_servers: dict) -> GoogleGenAIPlugin:
    from google.genai import Client as GeminiClient

    return GoogleGenAIPlugin(
        GeminiClient(api_key="fake-test-key"), mcp_servers=mcp_servers
    )


async def _activity_names(handle: Any) -> list[str]:
    names: list[str] = []
    async for e in handle.fetch_history_events():
        if e.HasField("activity_task_scheduled_event_attributes"):
            names.append(e.activity_task_scheduled_event_attributes.activity_type.name)
    return names


@pytest.fixture(autouse=True)
def _clear_mcp_connections():  # pyright: ignore[reportUnusedFunction]
    """Isolate the module-global MCP connection pool between tests."""
    from temporalio.contrib.google_genai import _mcp

    _mcp._CONNECTIONS.clear()
    yield
    _mcp._CONNECTIONS.clear()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class McpToolWorkflow:
    """generate_content grounded by an MCP tool, via the AFC loop.

    Takes the MCP server name as an argument so each test can use a distinct
    name (the worker-side connection pool is keyed by name and shared across
    activity invocations in the worker process).  The number of tool calls is
    driven entirely by the mocked model responses, not the workflow.
    """

    @workflow.run
    async def run(self, server_name: str, prompt: str) -> str:
        client = TemporalAsyncClient()
        session = TemporalMcpClientSession(
            server_name,
            cache_tools=True,
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(seconds=30)
            ),
        )
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[session]),
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# Client-side MCP tests
# ---------------------------------------------------------------------------


async def test_mcp_tool_discovery_and_call(client: Client):
    """The AFC loop discovers + calls an MCP tool through activities."""
    server = "echo_basic"
    new_client, _ = _apply_mcp_plugin(
        client,
        [
            make_function_call_response("echo", {"message": "hello"}),
            make_text_response("Done!"),
        ],
        mcp_servers={server: _echo_session},
    )

    async with new_worker(new_client, McpToolWorkflow) as worker:
        handle = await new_client.start_workflow(
            McpToolWorkflow.run,
            args=[server, "echo hello"],
            id=f"gemini-mcp-{uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        result = await handle.result()
        names = await _activity_names(handle)

    assert result == "Done!"
    assert names == [
        f"{server}-list-tools",
        "gemini_api_client_async_request",
        f"{server}-call-tool",
        "gemini_api_client_async_request",
    ]


async def test_mcp_connection_pooling(client: Client):
    """Two tool calls in one workflow reuse a single worker-side connection."""
    server = "echo_pool"
    factory = _CountingFactory()
    new_client, _ = _apply_mcp_plugin(
        client,
        [
            make_function_call_response("echo", {"message": "one"}),
            make_function_call_response("echo", {"message": "two"}),
            make_text_response("Done!"),
        ],
        mcp_servers={server: factory},
    )

    async with new_worker(new_client, McpToolWorkflow) as worker:
        handle = await new_client.start_workflow(
            McpToolWorkflow.run,
            args=[server, "echo twice"],
            id=f"gemini-mcp-pool-{uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert await handle.result() == "Done!"
        names = await _activity_names(handle)

    # list-tools + two call-tools all served by one lazily-opened connection.
    assert names.count(f"{server}-call-tool") == 2
    assert factory.opens == 1


async def test_mcp_full_schema_propagation(client: Client):
    """The model receives the MCP tool's full parameter schema, not just name."""
    server = "echo_schema"
    new_client, tracker = _apply_mcp_plugin(
        client,
        [
            make_function_call_response("echo", {"message": "hi"}),
            make_text_response("Done!"),
        ],
        mcp_servers={server: _echo_session},
    )

    async with new_worker(new_client, McpToolWorkflow) as worker:
        await new_client.execute_workflow(
            McpToolWorkflow.run,
            args=[server, "echo hi"],
            id=f"gemini-mcp-schema-{uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

    # The first generate request carries the tool declarations the SDK built
    # from the MCP list_tools result.
    first = tracker.requests[0].request_dict
    decls = first["tools"][0]["functionDeclarations"]  # type: ignore[index]
    echo_decl = next(d for d in decls if d["name"] == "echo")
    assert "parameters" in echo_decl
    assert "message" in echo_decl["parameters"]["properties"]


async def test_mcp_replay(client: Client):
    """A recorded MCP tool-loop history replays deterministically."""
    server = "echo_replay"
    new_client, _ = _apply_mcp_plugin(
        client,
        [
            make_function_call_response("echo", {"message": "hello"}),
            make_text_response("Done!"),
        ],
        mcp_servers={server: _echo_session},
    )

    async with new_worker(new_client, McpToolWorkflow) as worker:
        handle = await new_client.start_workflow(
            McpToolWorkflow.run,
            args=[server, "echo hello"],
            id=f"gemini-mcp-replay-{uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await handle.result()
        history = await handle.fetch_history()

    await Replayer(
        workflows=[McpToolWorkflow],
        plugins=[_replay_plugin({server: _echo_session})],
    ).replay_workflow(history)


async def test_mcp_side_effects(client: Client):
    """max_cached_workflows=0: exact ActivityTaskScheduled counts per type."""
    server = "echo_side"
    new_client, _ = _apply_mcp_plugin(
        client,
        [
            make_function_call_response("echo", {"message": "hello"}),
            make_text_response("Done!"),
        ],
        mcp_servers={server: _echo_session},
    )

    async with new_worker(
        new_client, McpToolWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await new_client.start_workflow(
            McpToolWorkflow.run,
            args=[server, "echo hello"],
            id=f"gemini-mcp-side-effects-{uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await handle.result()
        names = await _activity_names(handle)

    scheduled: dict[str, int] = {}
    for n in names:
        scheduled[n] = scheduled.get(n, 0) + 1
    assert scheduled == {
        f"{server}-list-tools": 1,
        "gemini_api_client_async_request": 2,
        f"{server}-call-tool": 1,
    }


# ---------------------------------------------------------------------------
# Server-side pass-through tests (no shim code)
# ---------------------------------------------------------------------------


def test_vertex_mcp_server_config_serializes():
    """Vertex server-side MCP config round-trips as plain request data."""
    tool = types.Tool(
        mcp_servers=[
            types.McpServer(
                name="weather",
                streamable_http_transport=types.StreamableHttpTransport(
                    url="https://example.com/mcp",
                ),
            )
        ]
    )
    config = types.GenerateContentConfig(tools=[tool])
    dumped = config.model_dump(mode="json", exclude_none=True)
    server = dumped["tools"][0]["mcp_servers"][0]
    assert server["name"] == "weather"
    assert server["streamable_http_transport"]["url"] == "https://example.com/mcp"


def test_interactions_mcp_steps_rehydrate():
    """Interactions API MCP step payloads rehydrate via construct_type."""
    from google.genai._interactions.types import InteractionSSEEvent

    call_event: Any = construct_type(
        type_=InteractionSSEEvent,
        value={
            "event_type": "step.start",
            "index": 0,
            "step": {
                "type": "mcp_server_tool_call",
                "id": "call-1",
                "name": "lookup",
                "server_name": "weather",
                "arguments": {"city": "Tokyo"},
            },
        },
    )
    assert call_event.step.type == "mcp_server_tool_call"
    assert call_event.step.server_name == "weather"
    assert call_event.step.arguments == {"city": "Tokyo"}

    result_event: Any = construct_type(
        type_=InteractionSSEEvent,
        value={
            "event_type": "step.start",
            "index": 0,
            "step": {
                "type": "mcp_server_tool_result",
                "call_id": "call-1",
                "name": "lookup",
                "server_name": "weather",
                "result": "sunny",
            },
        },
    )
    assert result_event.step.type == "mcp_server_tool_result"
    assert result_event.step.call_id == "call-1"
