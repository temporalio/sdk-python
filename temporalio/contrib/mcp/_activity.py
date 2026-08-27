# pyright: reportUnusedClass=false, reportUnusedFunction=false

from __future__ import annotations

import dataclasses
from typing import Any

from mcp.types import RequestParamsMeta


@dataclasses.dataclass
class _MCPRequest:
    factory_argument: Any = None


@dataclasses.dataclass
class _CallToolRequest(_MCPRequest):
    name: str = ""
    arguments: dict[str, Any] | None = None
    # Only call_tool has a caller that supplies request metadata: the OpenAI
    # Agents base MCPServer resolves it per tool call.
    meta: RequestParamsMeta | None = None


@dataclasses.dataclass
class _GetPromptRequest(_MCPRequest):
    name: str = ""
    arguments: dict[str, str] | None = None


@dataclasses.dataclass
class _ReadResourceRequest(_MCPRequest):
    uri: str = ""


def _activity_name(server: str, operation: str) -> str:
    return f"temporalio.contrib.mcp.{server}.{operation}"
