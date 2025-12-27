"""Temporal-aware agent creation functions."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.pregel import Pregel


def _mark_nodes_for_workflow_execution(graph: "Pregel") -> None:
    """Mark all nodes in a graph to run inline in the workflow.

    This modifies node metadata to set ``temporal.run_in_workflow = True``,
    which tells the TemporalLangGraphRunner to execute nodes directly
    instead of as activities.

    Args:
        graph: The compiled Pregel graph to modify.
    """
    for node_name, node in graph.nodes.items():
        # Skip __start__ as it already runs in workflow
        if node_name == "__start__":
            continue

        # Get or create metadata
        existing_metadata = getattr(node, "metadata", None) or {}
        existing_temporal = existing_metadata.get("temporal", {})

        # Set run_in_workflow flag
        node.metadata = {
            **existing_metadata,
            "temporal": {
                **existing_temporal,
                "run_in_workflow": True,
            },
        }


def _extract_activity_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Extract activity options from the nested format.

    activity_options() returns {"temporal": {...}}, so we need to extract
    the inner dict for passing to temporal_model/temporal_tool.
    """
    if options is None:
        return {}
    return options.get("temporal", {})


def create_durable_react_agent(
    model: "BaseChatModel",
    tools: Sequence["BaseTool | Any"],
    *,
    model_activity_options: dict[str, Any] | None = None,
    tool_activity_options: dict[str, Any] | None = None,
    # Pass-through to LangGraph's create_react_agent
    **kwargs: Any,
) -> "Pregel":
    """Create a ReAct agent with Temporal-durable model and tool execution.

    .. warning::
        This API is experimental and may change in future versions.

    This wraps ``langgraph.prebuilt.create_react_agent`` and automatically
    configures the model and tools for Temporal durability.

    When used with Temporal's LangGraph integration:
    - The agent nodes run inline in the workflow (deterministic orchestration)
    - Model calls execute as Temporal activities (durable LLM invocations)
    - Tool calls execute as Temporal activities (durable tool execution)

    This provides fine-grained durability where each LLM call and tool
    invocation is individually retryable and recoverable.

    Args:
        model: The chat model to use (will be wrapped with temporal_model).
        tools: List of tools for the agent (will be wrapped with temporal_tool).
        model_activity_options: Activity options for model calls, from
            ``activity_options()``. Defaults to 2 minute timeout.
        tool_activity_options: Activity options for tool calls, from
            ``activity_options()``. Defaults to 30 second timeout.
        **kwargs: Additional arguments passed to LangGraph's create_react_agent
            (e.g., state_schema, prompt, etc.).

    Returns:
        A compiled LangGraph Pregel graph with nodes marked to run in workflow.

    Example:
        .. code-block:: python

            from temporalio.contrib.langgraph import (
                create_durable_react_agent,
                activity_options,
            )

            agent = create_durable_react_agent(
                ChatOpenAI(model="gpt-4o-mini"),
                [search_tool, calculator_tool],
                model_activity_options=activity_options(
                    start_to_close_timeout=timedelta(minutes=5),
                ),
                tool_activity_options=activity_options(
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                ),
            )
    """
    from langgraph.prebuilt import create_react_agent as lg_create_react_agent

    from temporalio.contrib.langgraph._temporal_model import temporal_model
    from temporalio.contrib.langgraph._temporal_tool import temporal_tool

    # Extract options from activity_options() format
    model_opts = _extract_activity_options(model_activity_options)
    tool_opts = _extract_activity_options(tool_activity_options)

    # Apply defaults if not specified
    if "start_to_close_timeout" not in model_opts:
        model_opts["start_to_close_timeout"] = timedelta(minutes=2)
    if "start_to_close_timeout" not in tool_opts:
        tool_opts["start_to_close_timeout"] = timedelta(seconds=30)

    # Wrap model for durable LLM execution
    wrapped_model = temporal_model(model, **model_opts)

    # Wrap tools for durable execution
    wrapped_tools = [temporal_tool(tool, **tool_opts) for tool in tools]

    # Create the agent using LangGraph's implementation
    agent = lg_create_react_agent(wrapped_model, wrapped_tools, **kwargs)

    # Mark all nodes to run in workflow instead of as activities.
    # Since model and tools are wrapped with temporal_model/temporal_tool,
    # they will create their own activities when invoked.
    _mark_nodes_for_workflow_execution(agent)

    return agent


def create_durable_agent(
    model: "BaseChatModel",
    tools: Sequence["BaseTool | Any"],
    *,
    model_activity_options: dict[str, Any] | None = None,
    tool_activity_options: dict[str, Any] | None = None,
    # Pass-through to LangChain's create_agent
    **kwargs: Any,
) -> "Pregel":
    """Create an agent with Temporal-durable model and tool execution.

    .. warning::
        This API is experimental and may change in future versions.

    This wraps ``langchain.agents.create_agent`` (LangChain 1.0+) and
    automatically configures the model and tools for Temporal durability.

    When used with Temporal's LangGraph integration:
    - The agent nodes run inline in the workflow (deterministic orchestration)
    - Model calls execute as Temporal activities (durable LLM invocations)
    - Tool calls execute as Temporal activities (durable tool execution)

    This provides fine-grained durability where each LLM call and tool
    invocation is individually retryable and recoverable.

    Args:
        model: The chat model to use (will be wrapped with temporal_model).
        tools: List of tools for the agent (will be wrapped with temporal_tool).
        model_activity_options: Activity options for model calls, from
            ``activity_options()``. Defaults to 2 minute timeout.
        tool_activity_options: Activity options for tool calls, from
            ``activity_options()``. Defaults to 30 second timeout.
        **kwargs: Additional arguments passed to LangChain's create_agent
            (e.g., prompt, response_format, pre_model_hook, etc.).

    Returns:
        A compiled LangGraph Pregel graph with nodes marked to run in workflow.

    Example:
        .. code-block:: python

            from temporalio.contrib.langgraph import (
                create_durable_agent,
                activity_options,
            )

            agent = create_durable_agent(
                ChatOpenAI(model="gpt-4o-mini"),
                [search_tool, calculator_tool],
                model_activity_options=activity_options(
                    start_to_close_timeout=timedelta(minutes=5),
                ),
                tool_activity_options=activity_options(
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                ),
            )
    """
    from langchain.agents import create_agent as lc_create_agent

    from temporalio.contrib.langgraph._temporal_model import temporal_model
    from temporalio.contrib.langgraph._temporal_tool import temporal_tool

    # Extract options from activity_options() format
    model_opts = _extract_activity_options(model_activity_options)
    tool_opts = _extract_activity_options(tool_activity_options)

    # Apply defaults if not specified
    if "start_to_close_timeout" not in model_opts:
        model_opts["start_to_close_timeout"] = timedelta(minutes=2)
    if "start_to_close_timeout" not in tool_opts:
        tool_opts["start_to_close_timeout"] = timedelta(seconds=30)

    # Wrap model for durable LLM execution
    wrapped_model = temporal_model(model, **model_opts)

    # Wrap tools for durable execution
    wrapped_tools = [temporal_tool(tool, **tool_opts) for tool in tools]

    # Create the agent using LangChain's implementation
    agent = lc_create_agent(model=wrapped_model, tools=wrapped_tools, **kwargs)

    # Mark all nodes to run in workflow instead of as activities.
    # Since model and tools are wrapped with temporal_model/temporal_tool,
    # they will create their own activities when invoked.
    _mark_nodes_for_workflow_execution(agent)

    return agent
