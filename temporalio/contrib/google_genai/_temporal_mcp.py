"""Temporal-aware ``mcp.ClientSession`` shim.

``TemporalMcpClientSession`` is an ``mcp.ClientSession`` subclass that the user
places in ``generate_content(config=GenerateContentConfig(tools=[...]))`` just
like a real MCP session.  The Gemini SDK recognizes it via
``isinstance(tool, McpClientSession)`` and, inside ``generate_content`` (which
runs in the workflow), calls only two methods on it: ``list_tools()`` at tool
discovery and ``call_tool(name, arguments)`` in the automatic-function-calling
loop.  Both are overridden here to dispatch to the ``{server}-list-tools`` /
``{server}-call-tool`` activities, so the real ``mcp.ClientSession`` lives only
on the worker (registered via ``GoogleGenAIPlugin(mcp_servers=...)``).

This mirrors strands' ``TemporalMCPClient``: the handle carries only the server
name (which selects the worker-side factory) plus activity options; the
connection factory is never passed to the workflow or the root client.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.shared.session import ProgressFnT
from mcp.types import CallToolResult, ListToolsResult, PaginatedRequestParams

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import _McpCallToolRequest
from temporalio.workflow import ActivityConfig

_DEFAULT_MCP_TIMEOUT = timedelta(seconds=60)


class TemporalMcpClientSession(ClientSession):
    """``mcp.ClientSession`` whose tool discovery and calls run as activities.

    .. warning::
        This API is experimental and may change in future versions.

    Construct inside a workflow and pass it in the ``tools`` list of a
    ``generate_content`` call.  The matching server name must be registered on
    the worker via ``GoogleGenAIPlugin(mcp_servers={name: factory})``.

    ``cache_tools`` controls how often tools are listed.  When ``False`` (the
    default) the ``{server}-list-tools`` activity runs each time the SDK
    discovers tools (i.e. per ``generate_content`` call), so a server whose
    tools changed mid-workflow is picked up.  When ``True`` the first listing is
    cached on this instance and reused for its lifetime (replay-safe in-workflow
    state).

    Args:
        server_name: Name selecting the worker-side factory; also the activity
            prefix (``{server_name}-list-tools`` / ``{server_name}-call-tool``).
        cache_tools: Cache the tool listing after the first call.
        activity_config: Activity configuration (timeouts, retry policy, etc.)
            for the MCP activities.  Defaults to a 60-second
            ``start_to_close_timeout``.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        server_name: str,
        *,
        cache_tools: bool = False,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize without calling super (no real streams exist here)."""
        self._server_name = server_name
        self._cache_tools = cache_tools
        self._cached_tools: ListToolsResult | None = None
        self._activity_config: ActivityConfig = (
            ActivityConfig(start_to_close_timeout=_DEFAULT_MCP_TIMEOUT)
            if activity_config is None
            else activity_config
        )

    def _config(self, summary: str) -> ActivityConfig:
        config: ActivityConfig = {**self._activity_config}
        if "summary" not in config:
            config["summary"] = summary
        return config

    async def list_tools(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        cursor: str | None = None,
        *,
        params: PaginatedRequestParams | None = None,
    ) -> ListToolsResult:
        """List the server's tools via the ``{server}-list-tools`` activity."""
        if self._cache_tools and self._cached_tools is not None:
            return self._cached_tools
        result = await temporal_workflow.execute_activity(
            f"{self._server_name}-list-tools",
            result_type=ListToolsResult,
            **self._config(f"mcp.{self._server_name}.list_tools"),
        )
        if self._cache_tools:
            self._cached_tools = result
        return result

    async def call_tool(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: ProgressFnT | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Call a tool via the ``{server}-call-tool`` activity."""
        return await temporal_workflow.execute_activity(
            f"{self._server_name}-call-tool",
            _McpCallToolRequest(name=name, arguments=arguments or {}),
            result_type=CallToolResult,
            **self._config(f"mcp.{self._server_name}.call_tool:{name}"),
        )
