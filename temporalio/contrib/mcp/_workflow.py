"""Workflow-side durable MCP client."""

# pyright: reportUnusedClass=false

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ReadResourceResult,
    RequestParamsMeta,
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


class TemporalMCPClient:
    """A durable workflow proxy for an MCP Python SDK v2 client.

    This class is experimental and may change in future versions.

    Each operation executes as an Activity. Tool discovery is cached for the
    lifetime of this object by default. A non-None ``factory_argument`` selects
    a fresh worker-side client for every Activity. The argument is recorded in
    workflow history and must not contain secrets.

    This class intentionally does not inherit from ``mcp.Client`` or
    ``mcp.ClientSession``. It exposes the request/response operations that can
    cross a durable Activity boundary, while the real transport lifecycle stays
    on the worker.
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
        self._tools: ListToolsResult | None = None
        self._factory_argument = factory_argument

    @property
    def name(self) -> str:
        """Return the registered worker-side client name."""
        return self._name

    @property
    def cached_tools(self) -> ListToolsResult | None:
        """Return the cached tool result, or ``None`` when none is cached."""
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

    async def list_tools(self) -> ListToolsResult:
        """List all tools in one Activity, following every pagination cursor."""
        if self._cache_tools_list and self._tools is not None:
            return self._tools
        value = await self._execute("list-tools", self._request(), dict[str, Any])
        tools = ListToolsResult.model_validate(value)
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

    async def list_prompts(self) -> ListPromptsResult:
        """List all prompts in one Activity, following every pagination cursor."""
        value = await self._execute("list-prompts", self._request(), dict[str, Any])
        return ListPromptsResult.model_validate(value)

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

    async def list_resources(self) -> ListResourcesResult:
        """List all resources in one Activity, following every pagination cursor."""
        value = await self._execute("list-resources", self._request(), dict[str, Any])
        return ListResourcesResult.model_validate(value)

    async def list_resource_templates(self) -> ListResourceTemplatesResult:
        """List all resource templates, following every pagination cursor."""
        value = await self._execute(
            "list-resource-templates",
            self._request(),
            dict[str, Any],
        )
        return ListResourceTemplatesResult.model_validate(value)

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read a resource."""
        request = _ReadResourceRequest(
            factory_argument=self._factory_argument,
            uri=uri,
        )
        value = await self._execute("read-resource", request, dict[str, Any])
        return ReadResourceResult.model_validate(value)


# Kept as an internal alias while contrib integrations migrate to the public
# name. It is not exported from ``temporalio.contrib.mcp``.
_MCPClient = TemporalMCPClient
