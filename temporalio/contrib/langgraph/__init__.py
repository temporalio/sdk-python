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
from typing import Any, Optional

import temporalio.common
import temporalio.workflow

from temporalio.contrib.langgraph._graph_registry import get_graph
from temporalio.contrib.langgraph._models import StateSnapshot
from temporalio.contrib.langgraph._plugin import LangGraphPlugin
from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner


def temporal_node_metadata(
    *,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    task_queue: Optional[str] = None,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    cancellation_type: Optional[temporalio.workflow.ActivityCancellationType] = None,
    versioning_intent: Optional[temporalio.workflow.VersioningIntent] = None,
    summary: Optional[str] = None,
    priority: Optional[temporalio.common.Priority] = None,
    run_in_workflow: bool = False,
) -> dict[str, Any]:
    """Create typed metadata for LangGraph nodes with Temporal activity configuration.

    This helper provides type-safe configuration for LangGraph nodes when using
    the Temporal integration. It returns a properly structured metadata dict
    that can be passed to `graph.add_node()`.

    All parameters mirror the options available in `workflow.execute_activity()`.

    Args:
        schedule_to_close_timeout: Total time allowed from scheduling to completion,
            including retries. If not set, defaults to start_to_close_timeout.
        schedule_to_start_timeout: Maximum time from scheduling until the activity
            starts executing on a worker.
        start_to_close_timeout: Maximum time for a single activity execution attempt.
            This is the primary timeout for node execution.
        heartbeat_timeout: Maximum time between heartbeat requests. Required for
            activities that call `activity.heartbeat()`. If an activity doesn't
            heartbeat within this interval, it may be considered stalled and retried.
        task_queue: Route this node to a specific task queue (e.g., for GPU workers
            or high-memory workers). If None, uses the workflow's task queue.
        retry_policy: Temporal retry policy for the activity. If set, this takes
            precedence over LangGraph's native `retry_policy` parameter.
        cancellation_type: How cancellation of this activity is handled.
            See `ActivityCancellationType` for options.
        versioning_intent: Whether to run on a compatible worker Build ID.
            See `VersioningIntent` for options.
        summary: A human-readable summary of the activity for observability.
        priority: Priority for task queue ordering when tasks are backlogged.
        run_in_workflow: If True and `enable_workflow_execution=True` is set on
            `compile()`, this node will run directly in the workflow instead of
            as an activity. Only use for deterministic, non-I/O operations.

    Returns:
        A metadata dict with Temporal configuration under the "temporal" key.
        Can be merged with other metadata using the `|` operator.

    Example:
        Basic usage with timeouts:
            >>> graph.add_node(
            ...     "fetch_data",
            ...     fetch_from_api,
            ...     metadata=temporal_node_metadata(
            ...         start_to_close_timeout=timedelta(minutes=2),
            ...         heartbeat_timeout=timedelta(seconds=30),
            ...     ),
            ... )

        With retry policy:
            >>> from temporalio.common import RetryPolicy
            >>> graph.add_node(
            ...     "unreliable_api",
            ...     call_api,
            ...     metadata=temporal_node_metadata(
            ...         start_to_close_timeout=timedelta(minutes=5),
            ...         retry_policy=RetryPolicy(
            ...             initial_interval=timedelta(seconds=1),
            ...             maximum_attempts=5,
            ...             backoff_coefficient=2.0,
            ...         ),
            ...     ),
            ... )

        Routing to specialized workers:
            >>> graph.add_node(
            ...     "gpu_inference",
            ...     run_inference,
            ...     metadata=temporal_node_metadata(
            ...         start_to_close_timeout=timedelta(hours=1),
            ...         task_queue="gpu-workers",
            ...         heartbeat_timeout=timedelta(minutes=1),
            ...     ),
            ... )

        Combining with other metadata:
            >>> graph.add_node(
            ...     "process",
            ...     process_data,
            ...     metadata=temporal_node_metadata(
            ...         task_queue="gpu-workers",
            ...     ) | {"custom_key": "custom_value"},
            ... )
    """
    config: dict[str, Any] = {}
    if schedule_to_close_timeout is not None:
        config["schedule_to_close_timeout"] = schedule_to_close_timeout
    if schedule_to_start_timeout is not None:
        config["schedule_to_start_timeout"] = schedule_to_start_timeout
    if start_to_close_timeout is not None:
        config["start_to_close_timeout"] = start_to_close_timeout
    if heartbeat_timeout is not None:
        config["heartbeat_timeout"] = heartbeat_timeout
    if task_queue is not None:
        config["task_queue"] = task_queue
    if retry_policy is not None:
        config["retry_policy"] = retry_policy
    if cancellation_type is not None:
        config["cancellation_type"] = cancellation_type
    if versioning_intent is not None:
        config["versioning_intent"] = versioning_intent
    if summary is not None:
        config["summary"] = summary
    if priority is not None:
        config["priority"] = priority
    if run_in_workflow:
        config["run_in_workflow"] = True
    return {"temporal": config}


def compile(
    graph_id: str,
    *,
    defaults: Optional[dict[str, Any]] = None,
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
        defaults: Default activity configuration for all nodes, created via
            `temporal_node_metadata()`. Node-specific metadata overrides these.
            If not specified, defaults to 5 minute timeout and 3 retry attempts.
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
            >>> from temporalio.contrib.langgraph import LangGraphPlugin, temporal_node_metadata
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
            >>> from temporalio.contrib.langgraph import compile, temporal_node_metadata
            >>>
            >>> @workflow.defn
            >>> class WeatherAgentWorkflow:
            ...     @workflow.run
            ...     async def run(self, graph_id: str, query: str):
            ...         app = compile(
            ...             graph_id,
            ...             defaults=temporal_node_metadata(
            ...                 start_to_close_timeout=timedelta(minutes=10),
            ...                 task_queue="agent-workers",
            ...             ),
            ...         )
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
        defaults=defaults,
        enable_workflow_execution=enable_workflow_execution,
        checkpoint=checkpoint,
    )


__all__ = [
    "compile",
    "LangGraphPlugin",
    "StateSnapshot",
    "TemporalLangGraphRunner",
    "temporal_node_metadata",
]
