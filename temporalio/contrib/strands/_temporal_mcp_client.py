from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

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


async def populate_cache(server: str, client_factory: Callable[[], MCPClient]) -> None:
    """Connect to the MCP server, list tools, fill ``_TOOL_CACHE``."""
    client = client_factory()
    try:
        infos: list[_MCPToolInfo] = []
        for tool in await client.load_tools():
            if not isinstance(tool, MCPAgentTool):
                continue
            infos.append(
                _MCPToolInfo(
                    name=tool.mcp_tool.name,
                    description=tool.mcp_tool.description or "",
                    input_schema=tool.mcp_tool.inputSchema,
                    output_schema=tool.mcp_tool.outputSchema,
                )
            )
        _TOOL_CACHE[server] = infos
    finally:
        client.stop(None, None, None)


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
        client.start()
        try:
            return await client.call_tool_async(
                tool_use_id=args.tool_use_id,
                name=args.tool_name,
                arguments=args.arguments,
            )
        finally:
            client.stop(None, None, None)

    return call_tool
