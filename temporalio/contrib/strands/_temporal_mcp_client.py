from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_client import MCPClient
from strands.tools.mcp.mcp_types import MCPToolResult, MCPTransport
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


# Server name -> cached tool list. Populated by TemporalMCPClient._populate_cache
# at worker startup and read by TemporalMCPClient.load_tools() inside the
# workflow sandbox. ``temporalio`` is in the SDK's default sandbox passthrough,
# so this dict is shared between worker process and workflow execution.
_TOOL_CACHE: dict[str, list[_MCPToolInfo]] = {}


class TemporalMCPClient(ToolProvider):
    """An MCP server reference for use in both worker and workflow contexts.

    Construct once at module level. Pass to ``StrandsPlugin(mcp_clients=[...])``
    on the worker (which registers the ``{server}-call-tool`` activity and runs
    ``list_tools`` at worker startup), and to ``Agent(tools=[...])`` inside the
    workflow (which adds the discovered tools to the agent's registry).

    Construction does no I/O. The actual MCP connection happens worker-side at
    plugin startup; each tool call later runs as a Temporal activity.
    """

    def __init__(
        self,
        server: str,
        transport_factory: Callable[[], MCPTransport],
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
        """Configure the server name, transport factory, and activity options."""
        self._server = server
        self._transport_factory = transport_factory
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

    async def _populate_cache(self) -> None:
        """Connect to the MCP server, list tools, fill ``_TOOL_CACHE``."""
        client = MCPClient(self._transport_factory)
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
            _TOOL_CACHE[self._server] = infos
        finally:
            client.stop(None, None, None)

    def _clear_cache(self) -> None:
        _TOOL_CACHE.pop(self._server, None)

    def _get_activities(self) -> Sequence[Callable]:
        transport_factory = self._transport_factory

        @activity.defn(name=f"{self._server}-call-tool")
        async def call_tool(args: _CallToolArgs) -> MCPToolResult:
            client = MCPClient(transport_factory)
            client.start()
            try:
                return await client.call_tool_async(
                    tool_use_id=args.tool_use_id,
                    name=args.tool_name,
                    arguments=args.arguments,
                )
            finally:
                client.stop(None, None, None)

        return [call_tool]
