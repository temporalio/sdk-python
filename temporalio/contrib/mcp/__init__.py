"""Run MCP Python SDK v2 clients durably from Temporal workflows.

Register named worker-side clients with :class:`MCPPlugin`, then construct a
:class:`TemporalMCPClient` with the same name inside workflow code. MCP
operations execute as Activities; transports and credentials remain on the
worker.

This package is experimental and may change in future versions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from temporalio.contrib.mcp._plugin import MCPClientFactory, MCPPlugin
    from temporalio.contrib.mcp._workflow import TemporalMCPClient

__all__ = ["MCPClientFactory", "MCPPlugin", "TemporalMCPClient"]


def __getattr__(name: str) -> Any:
    """Load the MCP v2 public API only when a symbol is requested.

    The shared package also backs deprecated integration APIs that still import
    under MCP v1, so importing the package itself cannot require MCP v2.
    """
    if name in ("MCPClientFactory", "MCPPlugin"):
        from temporalio.contrib.mcp import _plugin

        return getattr(_plugin, name)
    if name == "TemporalMCPClient":
        from temporalio.contrib.mcp._workflow import TemporalMCPClient

        return TemporalMCPClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
