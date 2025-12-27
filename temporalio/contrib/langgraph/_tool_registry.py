"""Registry for LangChain tools used in Temporal activities."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from temporalio.contrib.langgraph._exceptions import (
    ToolAlreadyRegisteredError,
    tool_not_found_error,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

# Global registry for tools
_tool_registry: dict[str, "BaseTool"] = {}
_registry_lock = threading.Lock()


def register_tool(tool: "BaseTool") -> None:
    """Register a tool in the global registry."""
    with _registry_lock:
        existing = _tool_registry.get(tool.name)
        if existing is not None and existing is not tool:
            # Allow re-registration of the same tool instance
            if id(existing) != id(tool):
                # Check if it's functionally the same tool
                # (same name and description usually means same tool)
                if existing.description != tool.description:
                    raise ToolAlreadyRegisteredError(tool.name)
        _tool_registry[tool.name] = tool


def get_tool(name: str) -> "BaseTool":
    """Get a tool from the registry by name."""
    with _registry_lock:
        if name not in _tool_registry:
            available = list(_tool_registry.keys())
            raise tool_not_found_error(name, available)
        return _tool_registry[name]


def get_all_tools() -> dict[str, "BaseTool"]:
    """Get all registered tools."""
    with _registry_lock:
        return dict(_tool_registry)


def clear_registry() -> None:
    """Clear all registered tools. Mainly for testing."""
    with _registry_lock:
        _tool_registry.clear()
