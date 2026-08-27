# pyright: reportUnusedClass=false, reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, cast

from mcp import Client
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
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from temporalio.contrib.mcp._backend import _MCPBackendFactory


class _NotSupplied:
    pass


_NOT_SUPPLIED = _NotSupplied()


class _MCPClientBackend:
    """Adapt an MCP Python SDK v2 client to the shared Activity backend."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def __aenter__(self) -> "_MCPClientBackend":
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def cacheable(self) -> bool:
        return self._client.protocol_version in MODERN_PROTOCOL_VERSIONS

    async def _list_all(self, method: str, field: str) -> list[Any]:
        values: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        while True:
            options: dict[str, Any] = {"cursor": cursor}
            if method == "list_tools":
                options["cache_mode"] = "bypass"
            result = await getattr(self._client, method)(**options)
            values.extend(getattr(result, field))
            seen_cursors.add(cursor)
            next_cursor = result.next_cursor
            if next_cursor is None:
                return values
            if next_cursor in seen_cursors:
                raise ValueError("MCP server returned a repeated pagination cursor")
            cursor = next_cursor

    async def list_tools(self) -> list[Tool]:
        return await self._list_all("list_tools", "tools")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        meta: RequestParamsMeta | None,
    ) -> CallToolResult:
        return await self._client.call_tool(name, arguments, meta=meta)

    async def list_prompts(self) -> list[Prompt]:
        return await self._list_all("list_prompts", "prompts")

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult:
        return await self._client.get_prompt(name, arguments)

    async def list_resources(self) -> list[Resource]:
        return await self._list_all("list_resources", "resources")

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        return await self._list_all("list_resource_templates", "resource_templates")

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self._client.read_resource(uri)


def _mcp_client_backend_factory(
    factory: Callable[[], Client] | Callable[[Any], Client],
) -> _MCPBackendFactory:
    def create(argument: Any = _NOT_SUPPLIED) -> _MCPClientBackend:
        if argument is _NOT_SUPPLIED:
            client = cast(Callable[[], Client], factory)()
        else:
            client = cast(Callable[[Any], Client], factory)(argument)
        return _MCPClientBackend(client)

    return create
