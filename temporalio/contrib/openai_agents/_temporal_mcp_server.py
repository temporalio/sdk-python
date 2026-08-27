"""Adapter exposing durable MCP v2 clients as OpenAI Agents MCP servers."""

# pyright: reportUnusedClass=false

from __future__ import annotations

import inspect
from typing import Any, cast

from agents import AgentBase, RunContextWrapper
from agents.mcp import MCPServer
from agents.mcp.util import ToolFilter, ToolFilterContext
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ReadResourceResult,
    RequestParamsMeta,
    Tool,
)

from temporalio.contrib.mcp._workflow import _MCPClient


class _TemporalMCPServer(MCPServer):
    def __init__(
        self, client: _MCPClient, *, tool_filter: ToolFilter = None, **kwargs: Any
    ) -> None:
        self._client = client
        self._tool_filter = tool_filter
        super().__init__(**kwargs)

    @property
    def name(self) -> str:
        return self._client.name

    async def connect(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[Tool]:
        tools = await self._client.list_tools()
        if self._tool_filter is None:
            return tools
        if isinstance(self._tool_filter, dict):
            if "allowed_tool_names" in self._tool_filter:
                allowed = self._tool_filter["allowed_tool_names"]
                tools = [tool for tool in tools if tool.name in allowed]
            if "blocked_tool_names" in self._tool_filter:
                blocked = self._tool_filter["blocked_tool_names"]
                tools = [tool for tool in tools if tool.name not in blocked]
            return tools
        if run_context is None or agent is None:
            raise ValueError(
                "run_context and agent are required for dynamic MCP tool filtering"
            )
        context = ToolFilterContext(
            run_context=run_context, agent=agent, server_name=self.name
        )
        filtered: list[Tool] = []
        for tool in tools:
            included = self._tool_filter(context, tool)
            if inspect.isawaitable(included):
                included = await included
            if included:
                filtered.append(tool)
        return filtered

    @property
    def cached_tools(self) -> list[Tool] | None:
        return self._client.cached_tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        return await self._client.call_tool(
            tool_name, arguments, meta=cast(RequestParamsMeta | None, meta)
        )

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=await self._client.list_prompts())

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        string_arguments = (
            None
            if arguments is None
            else {key: str(value) for key, value in arguments.items()}
        )
        return await self._client.get_prompt(name, string_arguments)

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        if cursor is not None:
            raise ValueError(
                "Temporal MCP servers return fully paginated resource lists"
            )
        return ListResourcesResult(resources=await self._client.list_resources())

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        if cursor is not None:
            raise ValueError(
                "Temporal MCP servers return fully paginated resource template lists"
            )
        return ListResourceTemplatesResult(
            resource_templates=await self._client.list_resource_templates()
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self._client.read_resource(uri)
