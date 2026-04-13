"""Support for LLM tool-calling within Temporal activities.

This package provides :class:`ToolRegistry`, a unified interface for defining
LLM tools once and exporting provider-specific schemas for Anthropic or
OpenAI.  It also provides :func:`run_tool_loop`, a convenience function for
running a complete multi-turn tool-calling conversation from within a Temporal
activity.

For crash-safe multi-turn sessions with automatic heartbeat-based checkpoint
and resume, see :mod:`_session` (available after installing with the
``tool-registry`` extra).

Install::

    pip install "temporalio[tool-registry]"          # Anthropic only
    pip install "temporalio[tool-registry-openai]"   # Anthropic + OpenAI

Quickstart::

    from temporalio import activity
    from temporalio.contrib.tool_registry import ToolRegistry, run_tool_loop

    @activity.defn
    async def my_llm_activity(prompt: str) -> str:
        tools = ToolRegistry()

        @tools.handler({"name": "flag", "description": "Flag an issue",
                        "input_schema": {"type": "object",
                                         "properties": {"msg": {"type": "string"}}}})
        def handle_flag(inp: dict) -> str:
            results.append(inp["msg"])
            return "recorded"

        results: list[str] = []
        await run_tool_loop(provider="anthropic", system="You are ...",
                            prompt=prompt, tools=tools)
        return ", ".join(results)

See ``README.md`` in this package for full documentation.
"""

from temporalio.contrib.tool_registry._plugin import ToolRegistryPlugin
from temporalio.contrib.tool_registry._providers import run_tool_loop
from temporalio.contrib.tool_registry._registry import ToolRegistry
from temporalio.contrib.tool_registry._session import AgenticSession, agentic_session

from . import testing

__all__ = [
    "AgenticSession",
    "ToolRegistry",
    "ToolRegistryPlugin",
    "agentic_session",
    "run_tool_loop",
    "testing",
]
