"""Temporal integration for LangGraph.

This module provides seamless integration between LangGraph and Temporal,
enabling durable execution of LangGraph agents with automatic retries,
timeouts, and enterprise observability.

Quick Start:
    >>> from temporalio.client import Client
    >>> from temporalio.worker import Worker
    >>> from temporalio.contrib.langgraph import LangGraphPlugin, compile
    >>> from langgraph.graph import StateGraph
    >>>
    >>> # 1. Define your graph builder
    >>> def build_my_agent():
    ...     graph = StateGraph(MyState)
    ...     graph.add_node("process", process_data)
    ...     # ... add more nodes and edges ...
    ...     return graph.compile()
    >>>
    >>> # 2. Create plugin with registered graphs
    >>> plugin = LangGraphPlugin(
    ...     graphs={"my_agent": build_my_agent}
    ... )
    >>>
    >>> # 3. Connect client with plugin
    >>> client = await Client.connect("localhost:7233", plugins=[plugin])
    >>>
    >>> # 4. Define workflow using compile()
    >>> @workflow.defn
    >>> class MyAgentWorkflow:
    ...     @workflow.run
    ...     async def run(self, graph_id: str, input_data: dict):
    ...         app = compile(graph_id)
    ...         return await app.ainvoke(input_data)
    >>>
    >>> # 5. Create worker and run
    >>> worker = Worker(
    ...     client,
    ...     task_queue="langgraph-workers",
    ...     workflows=[MyAgentWorkflow],
    ... )

Key Components:
    - LangGraphPlugin: Temporal plugin for graph registration and activity setup
    - compile(): Function to get a TemporalLangGraphRunner for a registered graph
    - TemporalLangGraphRunner: Runner that executes graphs with Temporal activities
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from temporalio.contrib.langgraph._graph_registry import get_graph
from temporalio.contrib.langgraph._models import StateSnapshot
from temporalio.contrib.langgraph._plugin import LangGraphPlugin
from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner


def compile(
    graph_id: str,
    *,
    default_activity_timeout: Optional[timedelta] = None,
    default_max_retries: int = 3,
    default_task_queue: Optional[str] = None,
    enable_workflow_execution: bool = False,
    checkpoint: Optional[dict] = None,
) -> TemporalLangGraphRunner:
    """Compile a registered LangGraph graph for Temporal execution.

    This function retrieves a graph from the plugin registry and wraps it
    in a TemporalLangGraphRunner for durable execution within workflows.

    The graph must be registered with LangGraphPlugin before calling this
    function. Registration happens when the plugin is created:

        plugin = LangGraphPlugin(graphs={"my_graph": build_my_graph})

    Args:
        graph_id: ID of the graph registered with LangGraphPlugin.
            This should match a key in the `graphs` dict passed to the plugin.
        default_activity_timeout: Default timeout for node activities.
            Can be overridden per-node via metadata. Default: 5 minutes.
        default_max_retries: Default maximum retry attempts for activities.
            Can be overridden per-node via retry_policy. Default: 3.
        default_task_queue: Default task queue for activities.
            If None, uses the workflow's task queue.
        enable_workflow_execution: Enable hybrid execution mode.
            If True, nodes marked with metadata={"temporal": {"run_in_workflow": True}}
            will run directly in the workflow instead of as activities.
            Default: False (all nodes run as activities for safety).
        checkpoint: Optional checkpoint data from a previous execution's
            get_state().model_dump(). If provided, the runner will restore
            its internal state from this checkpoint, allowing continuation
            after a Temporal continue-as-new.

    Returns:
        A TemporalLangGraphRunner that can be used like a compiled graph.

    Raises:
        KeyError: If no graph with the given ID is registered.

    Example:
        Setup (main.py):
            >>> from temporalio.client import Client
            >>> from temporalio.contrib.langgraph import LangGraphPlugin
            >>>
            >>> def build_weather_agent():
            ...     graph = StateGraph(AgentState)
            ...     graph.add_node("fetch", fetch_data)
            ...     return graph.compile()
            >>>
            >>> plugin = LangGraphPlugin(
            ...     graphs={"weather_agent": build_weather_agent}
            ... )
            >>> client = await Client.connect("localhost:7233", plugins=[plugin])

        Usage (workflow.py):
            >>> from temporalio.contrib.langgraph import compile
            >>>
            >>> @workflow.defn
            >>> class WeatherAgentWorkflow:
            ...     @workflow.run
            ...     async def run(self, graph_id: str, query: str):
            ...         app = compile(graph_id)
            ...         return await app.ainvoke({"query": query})

        Usage with continue-as-new (workflow.py):
            >>> @workflow.defn
            >>> class LongRunningAgentWorkflow:
            ...     @workflow.run
            ...     async def run(self, input_data: dict, checkpoint: dict | None = None):
            ...         app = compile("my_graph", checkpoint=checkpoint)
            ...         result = await app.ainvoke(input_data)
            ...
            ...         # Check if we should continue-as-new
            ...         if workflow.info().get_current_history_length() > 10000:
            ...             snapshot = app.get_state()
            ...             workflow.continue_as_new(input_data, snapshot.model_dump())
            ...
            ...         return result
    """
    # Get graph from registry
    pregel = get_graph(graph_id)

    return TemporalLangGraphRunner(
        pregel,
        graph_id=graph_id,
        default_activity_timeout=default_activity_timeout,
        default_max_retries=default_max_retries,
        default_task_queue=default_task_queue,
        enable_workflow_execution=enable_workflow_execution,
        checkpoint=checkpoint,
    )


__all__ = [
    "compile",
    "LangGraphPlugin",
    "StateSnapshot",
    "TemporalLangGraphRunner",
]
