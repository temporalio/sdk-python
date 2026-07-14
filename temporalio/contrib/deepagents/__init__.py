"""Temporal plugin for LangChain Deep Agents.

Make an existing Deep Agent durable by adding one plugin: build your agent with
``create_deep_agent(...)`` inside a ``@workflow.defn`` and add
``plugins=[DeepAgentsPlugin(...)]`` to your Client or Worker. Each LLM call and
each I/O tool call becomes a Temporal activity, while the agent's control loop
runs — and deterministically replays — inside the workflow.

.. warning::
    This package is experimental and may change in future versions.

The public names are imported lazily so ``import temporalio.contrib.deepagents``
succeeds before LangChain is installed; touching a name that needs LangChain
imports it on first access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "DeepAgentsPlugin",
    "TemporalModel",
    "TemporalBackend",
    "activity_as_tool",
    "tool_as_activity",
    "run_deep_agent",
    "DeepAgentsWorkflowError",
]

if TYPE_CHECKING:
    from temporalio.contrib.deepagents._model import TemporalModel
    from temporalio.contrib.deepagents._plugin import DeepAgentsPlugin
    from temporalio.contrib.deepagents._tools import (
        TemporalBackend,
        activity_as_tool,
        tool_as_activity,
    )
    from temporalio.contrib.deepagents.workflow import (
        DeepAgentsWorkflowError,
        run_deep_agent,
    )


def __getattr__(name: str) -> object:
    if name == "DeepAgentsPlugin":
        from temporalio.contrib.deepagents._plugin import DeepAgentsPlugin

        return DeepAgentsPlugin
    if name == "TemporalModel":
        from temporalio.contrib.deepagents._model import TemporalModel

        return TemporalModel
    if name in ("TemporalBackend", "activity_as_tool", "tool_as_activity"):
        from temporalio.contrib.deepagents import _tools

        return getattr(_tools, name)
    if name in ("DeepAgentsWorkflowError", "run_deep_agent"):
        from temporalio.contrib.deepagents import workflow

        return getattr(workflow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
