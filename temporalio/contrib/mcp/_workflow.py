"""Workflow-side durable MCP client, shared by the contrib integrations.

Nothing here is public API. Reach it through an integration's own surface,
e.g. ``temporalio.contrib.openai_agents.workflow.temporal_mcp_server()``.
"""

# pyright: reportUnusedClass=false

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from mcp.types import (
    CallToolResult,
    GetPromptResult,
    Prompt,
    ReadResourceResult,
    RequestParamsMeta,
    Resource,
    ResourceTemplate,
    Tool,
)

from temporalio import workflow
from temporalio.contrib.mcp._activity import (
    _activity_name,
    _CallToolRequest,
    _GetPromptRequest,
    _MCPRequest,
    _ReadResourceRequest,
)
from temporalio.workflow import ActivityConfig


class _MCPClient:
    """A durable workflow proxy for an MCP Python SDK v2 client.

    Each operation executes as an Activity. Tool discovery is cached for the
    lifetime of this object by default. A non-None ``factory_argument`` selects
    a fresh worker-side client for every Activity. The argument is recorded in
    workflow history and must not contain secrets.
    """

    def __init__(
        self,
        name: str,
        *,
        activity_config: ActivityConfig | None = None,
        cache_tools_list: bool = True,
        factory_argument: Any = None,
    ) -> None:
        """Configure the registered name and MCP operation Activities."""
        self._name = name
        self._activity_config: ActivityConfig = activity_config or {
            "start_to_close_timeout": timedelta(minutes=1)
        }
        self._cache_tools_list = cache_tools_list
        self._tools: list[Tool] | None = None
        self._factory_argument = factory_argument

    @property
    def name(self) -> str:
        """Return the registered worker-side client name."""
        return self._name

    @property
    def cached_tools(self) -> list[Tool] | None:
        """Return the cached tools, or ``None`` when no list is cached."""
        return self._tools

    def _request(self) -> _MCPRequest:
        return _MCPRequest(factory_argument=self._factory_argument)

    async def _execute(
        self, operation: str, request: _MCPRequest, result_type: Any
    ) -> Any:
        return await workflow.execute_activity(
            _activity_name(self._name, operation),
            dataclasses.asdict(request),
            result_type=result_type,
            **self._activity_config,
        )

    async def list_tools(self) -> list[Tool]:
        """List all tools, following every pagination cursor."""
        if self._cache_tools_list and self._tools is not None:
            return self._tools
        values = await self._execute(
            "list-tools", self._request(), list[dict[str, Any]]
        )
        tools = [Tool.model_validate(value) for value in values]
        if self._cache_tools_list:
            self._tools = tools
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: RequestParamsMeta | None = None,
    ) -> CallToolResult:
        """Call a tool.

        ``meta`` is the only request metadata carried by this client, because
        ``call_tool`` is the only operation with a caller that supplies it.
        """
        request = _CallToolRequest(
            factory_argument=self._factory_argument,
            meta=meta,
            name=name,
            arguments=arguments,
        )
        value = await self._execute("call-tool", request, dict[str, Any])
        return CallToolResult.model_validate(value)

    async def list_prompts(self) -> list[Prompt]:
        """List all prompts, following every pagination cursor."""
        values = await self._execute(
            "list-prompts", self._request(), list[dict[str, Any]]
        )
        return [Prompt.model_validate(value) for value in values]

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> GetPromptResult:
        """Get a prompt."""
        request = _GetPromptRequest(
            factory_argument=self._factory_argument,
            name=name,
            arguments=arguments,
        )
        value = await self._execute("get-prompt", request, dict[str, Any])
        return GetPromptResult.model_validate(value)

    async def list_resources(self) -> list[Resource]:
        """List all resources, following every pagination cursor."""
        values = await self._execute(
            "list-resources", self._request(), list[dict[str, Any]]
        )
        return [Resource.model_validate(value) for value in values]

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        """List all resource templates, following every pagination cursor."""
        values = await self._execute(
            "list-resource-templates",
            self._request(),
            list[dict[str, Any]],
        )
        return [ResourceTemplate.model_validate(value) for value in values]

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read a resource."""
        request = _ReadResourceRequest(
            factory_argument=self._factory_argument,
            uri=uri,
        )
        value = await self._execute("read-resource", request, dict[str, Any])
        return ReadResourceResult.model_validate(value)
