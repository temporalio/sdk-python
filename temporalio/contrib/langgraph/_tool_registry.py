"""Registry for LangChain tools used in Temporal activities.

This module provides a global registry for tools that are wrapped with
temporal_tool(). The registry allows the execute_tool activity to look up
tools by name for execution.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

# Global registry for tools
_tool_registry: dict[str, "BaseTool"] = {}
_registry_lock = threading.Lock()


def register_tool(tool: "BaseTool") -> None:
    """Register a tool in the global registry.

    Args:
        tool: The LangChain tool to register.

    Raises:
        ValueError: If a different tool with the same name is already registered.
    """
    with _registry_lock:
        existing = _tool_registry.get(tool.name)
        if existing is not None and existing is not tool:
            # Allow re-registration of the same tool instance
            if id(existing) != id(tool):
                # Check if it's functionally the same tool
                # (same name and description usually means same tool)
                if existing.description != tool.description:
                    raise ValueError(
                        f"Tool '{tool.name}' is already registered with a different "
                        f"implementation. Each tool name must be unique."
                    )
        _tool_registry[tool.name] = tool


def get_tool(name: str) -> "BaseTool":
    """Get a tool from the registry by name.

    Args:
        name: The name of the tool to retrieve.

    Returns:
        The registered BaseTool instance.

    Raises:
        KeyError: If no tool with the given name is registered.
    """
    with _registry_lock:
        if name not in _tool_registry:
            available = list(_tool_registry.keys())
            raise KeyError(
                f"Tool '{name}' not found in registry. "
                f"Available tools: {available}. "
                f"Make sure the tool is wrapped with temporal_tool() and "
                f"the graph is registered with LangGraphPlugin."
            )
        return _tool_registry[name]


def get_all_tools() -> dict[str, "BaseTool"]:
    """Get all registered tools.

    Returns:
        A copy of the tool registry dict.
    """
    with _registry_lock:
        return dict(_tool_registry)


def clear_registry() -> None:
    """Clear all registered tools. Mainly for testing."""
    with _registry_lock:
        _tool_registry.clear()
