"""Temporal-aware agent creation functions."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.pregel import Pregel

    from temporalio.common import Priority, RetryPolicy
    from temporalio.workflow import ActivityCancellationType, VersioningIntent


def _build_common_activity_options(
    schedule_to_close_timeout: timedelta | None,
    schedule_to_start_timeout: timedelta | None,
    heartbeat_timeout: timedelta | None,
    task_queue: str | None,
    cancellation_type: "ActivityCancellationType | None",
    versioning_intent: "VersioningIntent | None",
    priority: "Priority | None",
) -> dict[str, Any]:
    """Build common activity options dict."""
    options: dict[str, Any] = {}
    if schedule_to_close_timeout is not None:
        options["schedule_to_close_timeout"] = schedule_to_close_timeout
    if schedule_to_start_timeout is not None:
        options["schedule_to_start_timeout"] = schedule_to_start_timeout
    if heartbeat_timeout is not None:
        options["heartbeat_timeout"] = heartbeat_timeout
    if task_queue is not None:
        options["task_queue"] = task_queue
    if cancellation_type is not None:
        options["cancellation_type"] = cancellation_type
    if versioning_intent is not None:
        options["versioning_intent"] = versioning_intent
    if priority is not None:
        options["priority"] = priority
    return options


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


def create_durable_react_agent(
    model: "BaseChatModel",
    tools: Sequence["BaseTool | Any"],
    *,
    # Model activity options
    model_start_to_close_timeout: timedelta = timedelta(minutes=2),
    model_retry_policy: "RetryPolicy | None" = None,
    # Tool activity options
    tool_start_to_close_timeout: timedelta = timedelta(seconds=30),
    tool_retry_policy: "RetryPolicy | None" = None,
    # Common activity options
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    task_queue: str | None = None,
    cancellation_type: "ActivityCancellationType | None" = None,
    versioning_intent: "VersioningIntent | None" = None,
    priority: "Priority | None" = None,
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
        model_start_to_close_timeout: Timeout for model activity execution.
        model_retry_policy: Retry policy for model activities.
        tool_start_to_close_timeout: Timeout for tool activity execution.
        tool_retry_policy: Retry policy for tool activities.
        schedule_to_close_timeout: Max time from scheduling to completion.
        schedule_to_start_timeout: Max time from scheduling to start.
        heartbeat_timeout: Heartbeat timeout for activities.
        task_queue: Task queue for activities (defaults to workflow's queue).
        cancellation_type: How to handle activity cancellation.
        versioning_intent: Versioning intent for activities.
        priority: Priority for activities.
        **kwargs: Additional arguments passed to LangGraph's create_react_agent
            (e.g., state_schema, prompt, etc.).

    Returns:
        A compiled LangGraph Pregel graph with nodes marked to run in workflow.

    Example:
        .. code-block:: python

            from temporalio.contrib.langgraph import create_durable_react_agent

            agent = create_durable_react_agent(
                ChatOpenAI(model="gpt-4o-mini"),
                [search_tool, calculator_tool],
            )
    """
    from langgraph.prebuilt import create_react_agent as lg_create_react_agent

    from temporalio.contrib.langgraph._temporal_model import temporal_model
    from temporalio.contrib.langgraph._temporal_tool import temporal_tool

    # Build common activity options
    common_options = _build_common_activity_options(
        schedule_to_close_timeout,
        schedule_to_start_timeout,
        heartbeat_timeout,
        task_queue,
        cancellation_type,
        versioning_intent,
        priority,
    )

    # Wrap model for durable LLM execution
    wrapped_model = temporal_model(
        model,
        start_to_close_timeout=model_start_to_close_timeout,
        retry_policy=model_retry_policy,
        **common_options,
    )

    # Wrap tools for durable execution
    wrapped_tools = [
        temporal_tool(
            tool,
            start_to_close_timeout=tool_start_to_close_timeout,
            retry_policy=tool_retry_policy,
            **common_options,
        )
        for tool in tools
    ]

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
    # Model activity options
    model_start_to_close_timeout: timedelta = timedelta(minutes=2),
    model_retry_policy: "RetryPolicy | None" = None,
    # Tool activity options
    tool_start_to_close_timeout: timedelta = timedelta(seconds=30),
    tool_retry_policy: "RetryPolicy | None" = None,
    # Common activity options
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    task_queue: str | None = None,
    cancellation_type: "ActivityCancellationType | None" = None,
    versioning_intent: "VersioningIntent | None" = None,
    priority: "Priority | None" = None,
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
        model_start_to_close_timeout: Timeout for model activity execution.
        model_retry_policy: Retry policy for model activities.
        tool_start_to_close_timeout: Timeout for tool activity execution.
        tool_retry_policy: Retry policy for tool activities.
        schedule_to_close_timeout: Max time from scheduling to completion.
        schedule_to_start_timeout: Max time from scheduling to start.
        heartbeat_timeout: Heartbeat timeout for activities.
        task_queue: Task queue for activities (defaults to workflow's queue).
        cancellation_type: How to handle activity cancellation.
        versioning_intent: Versioning intent for activities.
        priority: Priority for activities.
        **kwargs: Additional arguments passed to LangChain's create_agent
            (e.g., prompt, response_format, pre_model_hook, etc.).

    Returns:
        A compiled LangGraph Pregel graph with nodes marked to run in workflow.

    Example:
        .. code-block:: python

            from temporalio.contrib.langgraph import create_durable_agent

            agent = create_durable_agent(
                ChatOpenAI(model="gpt-4o-mini"),
                [search_tool, calculator_tool],
            )
    """
    from langchain.agents import create_agent as lc_create_agent

    from temporalio.contrib.langgraph._temporal_model import temporal_model
    from temporalio.contrib.langgraph._temporal_tool import temporal_tool

    # Build common activity options
    common_options = _build_common_activity_options(
        schedule_to_close_timeout,
        schedule_to_start_timeout,
        heartbeat_timeout,
        task_queue,
        cancellation_type,
        versioning_intent,
        priority,
    )

    # Wrap model for durable LLM execution
    wrapped_model = temporal_model(
        model,
        start_to_close_timeout=model_start_to_close_timeout,
        retry_policy=model_retry_policy,
        **common_options,
    )

    # Wrap tools for durable execution
    wrapped_tools = [
        temporal_tool(
            tool,
            start_to_close_timeout=tool_start_to_close_timeout,
            retry_policy=tool_retry_policy,
            **common_options,
        )
        for tool in tools
    ]

    # Create the agent using LangChain's implementation
    agent = lc_create_agent(model=wrapped_model, tools=wrapped_tools, **kwargs)

    # Mark all nodes to run in workflow instead of as activities.
    # Since model and tools are wrapped with temporal_model/temporal_tool,
    # they will create their own activities when invoked.
    _mark_nodes_for_workflow_execution(agent)

    return agent
