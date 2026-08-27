# pyright: reportUnusedClass=false

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, Protocol

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


class _MCPBackend(Protocol):
    """Normalized worker-side MCP operations used by the Activity layer."""

    async def __aenter__(self) -> "_MCPBackend": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    @property
    def cacheable(self) -> bool: ...

    async def list_tools(self) -> list[Tool]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        meta: RequestParamsMeta | None,
    ) -> CallToolResult: ...

    async def list_prompts(self) -> list[Prompt]: ...

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult: ...

    async def list_resources(self) -> list[Resource]: ...

    async def list_resource_templates(self) -> list[ResourceTemplate]: ...

    async def read_resource(self, uri: str) -> ReadResourceResult: ...


_MCPBackendFactory = Callable[[], _MCPBackend] | Callable[[Any], _MCPBackend]
