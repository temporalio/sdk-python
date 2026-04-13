"""ToolRegistry: define LLM tools once, export provider-specific schemas.

A ``ToolRegistry`` stores a mapping from tool name to its definition (in
Anthropic's ``tool_use`` format) and a callable handler.  The same registry
can be converted to Anthropic-format or OpenAI-format schemas for use with
either provider's client library, and dispatches incoming tool calls to the
registered handler.

Example::

    tools = ToolRegistry()

    @tools.handler({"name": "flag_issue", "description": "Flag a diagram issue",
                    "input_schema": {"type": "object",
                                     "properties": {"description": {"type": "string"}},
                                     "required": ["description"]}})
    def handle_flag_issue(inp: dict) -> str:
        issues.append(inp["description"])
        return "recorded"

    # Use with Anthropic
    response = client.messages.create(tools=tools.to_anthropic(), ...)

    # Use with OpenAI
    response = client.chat.completions.create(tools=tools.to_openai(), ...)

    # Dispatch a tool call returned by the model
    result = tools.dispatch(tool_name, tool_input)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ToolRegistry:
    """Registry mapping tool names to definitions and handlers.

    Tools are registered in Anthropic's ``tool_use`` JSON format (with
    ``name``, ``description``, and ``input_schema`` keys).  The registry
    can then export the same tools for Anthropic or OpenAI providers, and
    dispatch incoming tool calls to the appropriate handler.
    """

    def __init__(self) -> None:
        """Initialize ToolRegistry with empty definitions and handlers."""
        self._definitions: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def handler(self, definition: dict[str, Any]) -> Callable:
        """Decorator that registers a handler for the given tool definition.

        Args:
            definition: Tool definition in Anthropic ``tool_use`` format —
                must contain ``name``, ``description``, and ``input_schema``.

        Returns:
            A decorator that registers the wrapped callable and returns it
            unchanged.

        Example::

            @tools.handler({"name": "my_tool", "description": "...",
                            "input_schema": {...}})
            def handle_my_tool(inp: dict) -> str:
                return "result"
        """

        def decorator(fn: Callable[[dict[str, Any]], str]) -> Callable:
            self._definitions.append(definition)
            self._handlers[definition["name"]] = fn
            return fn

        return decorator

    @classmethod
    def from_mcp_tools(cls, tools: list[Any]) -> "ToolRegistry":
        """Build a ``ToolRegistry`` from a list of MCP ``Tool`` objects.

        Each MCP tool exposes ``name``, ``description``, and
        ``inputSchema`` attributes.  The resulting registry has no-op
        handlers (returning an empty string) — callers must replace them
        via :meth:`handler` or :meth:`dispatch` overrides as needed.

        Args:
            tools: List of ``mcp.Tool`` (or any object with ``name``,
                ``description``, and ``inputSchema`` attributes).

        Returns:
            A new :class:`ToolRegistry` with definitions populated from
            the MCP tools.
        """
        registry = cls()
        for tool in tools:
            defn = {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema
                or {"type": "object", "properties": {}},
            }
            registry._definitions.append(defn)
            registry._handlers[tool.name] = lambda _inp: ""
        return registry

    # ── Schema export ─────────────────────────────────────────────────────────

    def to_anthropic(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic ``tool_use`` format.

        The definitions are returned exactly as registered — no conversion
        needed because the registry stores them in Anthropic format.

        Returns:
            List of dicts with ``name``, ``description``, and
            ``input_schema`` keys.
        """
        return list(self._definitions)

    def to_openai(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format.

        Converts each Anthropic-format definition to the OpenAI
        ``{"type": "function", "function": {...}}`` shape, mapping
        ``input_schema`` to ``parameters``.

        Returns:
            List of dicts in OpenAI tool format.
        """
        result = []
        for defn in self._definitions:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": defn["name"],
                        "description": defn["description"],
                        "parameters": defn["input_schema"],
                    },
                }
            )
        return result

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, name: str, input_dict: dict[str, Any]) -> str:
        """Call the handler registered for ``name`` with ``input_dict``.

        Args:
            name: Tool name as returned by the model.
            input_dict: Parsed tool input as a plain ``dict``.

        Returns:
            String result from the handler.

        Raises:
            KeyError: If no handler is registered for ``name``.
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"Unknown tool: {name!r}")
        return handler(input_dict)
