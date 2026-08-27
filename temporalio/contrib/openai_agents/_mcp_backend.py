# pyright: reportUnusedClass=false, reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, cast

from agents.mcp import MCPServer
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

_MCPServerFactory = Callable[[], MCPServer] | Callable[[Any], MCPServer]


class _NotSupplied:
    pass


_NOT_SUPPLIED = _NotSupplied()


class _OpenAIMCPServerBackend:
    """Adapt an OpenAI Agents MCPServer to durable MCP Activities."""

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    async def __aenter__(self) -> "_OpenAIMCPServerBackend":
        await self._server.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._server.cleanup()

    @property
    def cacheable(self) -> bool:
        session = getattr(self._server, "session", None)
        return getattr(session, "protocol_version", None) in MODERN_PROTOCOL_VERSIONS

    async def list_tools(self) -> list[Tool]:
        return await self._server.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        meta: RequestParamsMeta | None,
    ) -> CallToolResult:
        return await self._server.call_tool(
            name, arguments, cast(dict[str, Any] | None, meta)
        )

    async def list_prompts(self) -> list[Prompt]:
        return (await self._server.list_prompts()).prompts

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult:
        return await self._server.get_prompt(name, arguments)

    async def list_resources(self) -> list[Resource]:
        return await self._list_all("list_resources", "resources")

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        return await self._list_all("list_resource_templates", "resource_templates")

    async def _list_all(self, method: str, field: str) -> list[Any]:
        values: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        while True:
            result = await getattr(self._server, method)(cursor)
            values.extend(getattr(result, field))
            seen_cursors.add(cursor)
            next_cursor = result.next_cursor
            if next_cursor is None:
                return values
            if next_cursor in seen_cursors:
                raise ValueError("MCP server returned a repeated pagination cursor")
            cursor = next_cursor

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self._server.read_resource(uri)


def _mcp_server_backend_factory(factory: _MCPServerFactory) -> _MCPBackendFactory:
    def create(argument: Any = _NOT_SUPPLIED) -> _OpenAIMCPServerBackend:
        if argument is _NOT_SUPPLIED:
            server = cast(Callable[[], MCPServer], factory)()
        else:
            server = cast(Callable[[Any], MCPServer], factory)(argument)
        return _OpenAIMCPServerBackend(server)

    return create
