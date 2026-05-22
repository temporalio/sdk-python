from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.types import PaginatedRequestParams, Tool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient
from strands.tools.mcp.mcp_types import MCPToolResult
from strands.tools.tool_provider import ToolProvider
from strands.types.tools import AgentTool

from temporalio import activity
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@dataclass
class _MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass
class _CallToolArgs:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str = ""


# Server name -> cached tool list. Populated by ``_populate_cache`` at worker
# startup and read by ``TemporalMCPClient.load_tools()`` inside the workflow
# sandbox. ``temporalio`` is in the SDK's default sandbox passthrough, so this
# dict is shared between worker process and workflow execution.
_TOOL_CACHE: dict[str, list[_MCPToolInfo]] = {}


class TemporalMCPClient(ToolProvider):
    """Workflow-side handle to an MCP server registered on the worker.

    The transport factory and tool discovery live worker-side via
    ``StrandsPlugin(mcp_clients={"server": lambda: ...})``. This handle only
    carries the server name (which selects the registered factory) and the
    per-call activity options.

    Construct once at module level and pass to ``TemporalAgent(tools=[...])``
    inside the workflow. Multiple handles may reference the same server name
    with different activity options.
    """

    def __init__(
        self,
        server: str,
        *,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
    ) -> None:
        """Configure the server name and activity options."""
        self._server = server
        self._options: dict[str, Any] = {
            "task_queue": task_queue,
            "schedule_to_close_timeout": schedule_to_close_timeout,
            "schedule_to_start_timeout": schedule_to_start_timeout,
            "start_to_close_timeout": start_to_close_timeout,
            "heartbeat_timeout": heartbeat_timeout,
            "retry_policy": retry_policy,
            "cancellation_type": cancellation_type,
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }

    @property
    def server(self) -> str:
        """MCP server name used as the activity prefix."""
        return self._server

    async def load_tools(self, **_kwargs: Any) -> Sequence[AgentTool]:
        """Return TemporalMCPTool wrappers for tools cached at worker startup."""
        from ._temporal_mcp_tool import TemporalMCPTool

        infos = _TOOL_CACHE.get(self._server, [])
        return [TemporalMCPTool(self._server, info, self._options) for info in infos]

    def add_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None

    def remove_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None


# Use MCP sessions directly instead of MCPClient's background-thread helpers.
# Those helpers route calls through cross-loop futures that are unreliable on
# Python 3.10 when invoked from Temporal's async worker/activity event loops.
async def _list_mcp_tools(client: MCPClient) -> Sequence[Tool]:
    async with client._transport_callable() as (read_stream, write_stream, *_):
        async with ClientSession(
            read_stream,
            write_stream,
            elicitation_callback=client._elicitation_callback,
        ) as session:
            await session.initialize()
            tools: list[Tool] = []
            pagination_token = None
            while True:
                page = await session.list_tools(
                    params=PaginatedRequestParams(cursor=pagination_token)
                    if pagination_token is not None
                    else None
                )
                tools.extend(page.tools)
                pagination_token = page.nextCursor
                if pagination_token is None:
                    return tools


def _agent_tool_for_filtering(client: MCPClient, tool: Tool) -> MCPAgentTool:
    if client._prefix:
        return MCPAgentTool(tool, client, name_override=f"{client._prefix}_{tool.name}")
    return MCPAgentTool(tool, client)


async def populate_cache(server: str, client_factory: Callable[[], MCPClient]) -> None:
    """Connect to the MCP server, list tools, fill ``_TOOL_CACHE``."""
    client = client_factory()
    infos: list[_MCPToolInfo] = []
    for tool in await _list_mcp_tools(client):
        if not client._should_include_tool_with_filters(
            _agent_tool_for_filtering(client, tool),
            client._tool_filters,
        ):
            continue
        infos.append(
            _MCPToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
                output_schema=tool.outputSchema,
            )
        )
    _TOOL_CACHE[server] = infos


def clear_cache(server: str) -> None:
    """Drop the cached tool list for ``server``."""
    _TOOL_CACHE.pop(server, None)


def build_call_tool_activity(
    server: str, client_factory: Callable[[], MCPClient]
) -> Callable:
    """Return the per-server ``{server}-call-tool`` activity for registration."""

    @activity.defn(name=f"{server}-call-tool")
    async def call_tool(args: _CallToolArgs) -> MCPToolResult:
        client = client_factory()
        try:
            async with client._transport_callable() as (read_stream, write_stream, *_):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    elicitation_callback=client._elicitation_callback,
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(args.tool_name, args.arguments)
                    return client._handle_tool_result(args.tool_use_id, result)
        except Exception as err:
            return client._handle_tool_execution_error(args.tool_use_id, err)

    return call_tool
