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

from temporalio.contrib.langgraph._graph_registry import (
    get_default_activity_options,
    get_graph,
    get_per_node_activity_options,
)
from temporalio.contrib.langgraph._model_registry import (
    register_model,
    register_model_factory,
)
from temporalio.contrib.langgraph._models import StateSnapshot
from temporalio.contrib.langgraph._plugin import LangGraphPlugin
from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner
from temporalio.contrib.langgraph._temporal_model import temporal_model
from temporalio.contrib.langgraph._temporal_tool import temporal_tool
from temporalio.contrib.langgraph._tool_registry import register_tool


def node_activity_options(
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
) -> dict[str, Any]:
    """Create activity options for LangGraph nodes.

    This helper provides type-safe configuration for LangGraph nodes when using
    the Temporal integration. It returns a properly structured dict that can be
    passed to `graph.add_node(metadata=...)` or to `compile()` parameters.

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

    Returns:
        A metadata dict with Temporal configuration under the "temporal" key.
        Can be merged with other metadata using the `|` operator.

    Example:
        Basic usage with timeouts:
            >>> graph.add_node(
            ...     "fetch_data",
            ...     fetch_from_api,
            ...     metadata=node_activity_options(
            ...         start_to_close_timeout=timedelta(minutes=2),
            ...         heartbeat_timeout=timedelta(seconds=30),
            ...     ),
            ... )

        With retry policy:
            >>> from temporalio.common import RetryPolicy
            >>> graph.add_node(
            ...     "unreliable_api",
            ...     call_api,
            ...     metadata=node_activity_options(
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
            ...     metadata=node_activity_options(
            ...         start_to_close_timeout=timedelta(hours=1),
            ...         task_queue="gpu-workers",
            ...         heartbeat_timeout=timedelta(minutes=1),
            ...     ),
            ... )

        Combining with other metadata:
            >>> graph.add_node(
            ...     "process",
            ...     process_data,
            ...     metadata=node_activity_options(
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
    return {"temporal": config}


def temporal_node_metadata(
    *,
    activity_options: Optional[dict[str, Any]] = None,
    run_in_workflow: bool = False,
) -> dict[str, Any]:
    """Create complete node metadata for Temporal LangGraph integration.

    This helper combines activity options with workflow execution flags into
    a single metadata dict. Use this when you need to specify both activity
    configuration and workflow execution behavior for a node.

    Args:
        activity_options: Activity options from ``node_activity_options()``.
            If provided, these will be merged into the result.
        run_in_workflow: If True and ``enable_workflow_execution=True`` is set
            on ``compile()``, this node will run directly in the workflow
            instead of as an activity. Only use for deterministic, non-I/O
            operations like validation, routing logic, or pure computations.

    Returns:
        A metadata dict with Temporal configuration under the "temporal" key.
        Can be merged with other metadata using the ``|`` operator.

    Example:
        Mark a node to run in workflow (deterministic operations):

            >>> graph.add_node(
            ...     "validate",
            ...     validate_input,
            ...     metadata=temporal_node_metadata(run_in_workflow=True),
            ... )

        Combine activity options with workflow execution:

            >>> graph.add_node(
            ...     "process",
            ...     process_data,
            ...     metadata=temporal_node_metadata(
            ...         activity_options=node_activity_options(
            ...             start_to_close_timeout=timedelta(minutes=5),
            ...             task_queue="gpu-workers",
            ...         ),
            ...         run_in_workflow=False,  # Run as activity (default)
            ...     ),
            ... )

        Activity options only (equivalent to node_activity_options directly):

            >>> graph.add_node(
            ...     "fetch",
            ...     fetch_data,
            ...     metadata=temporal_node_metadata(
            ...         activity_options=node_activity_options(
            ...             start_to_close_timeout=timedelta(minutes=2),
            ...         ),
            ...     ),
            ... )

    Note:
        For nodes that only need activity options without ``run_in_workflow``,
        you can use ``node_activity_options()`` directly as metadata.
    """
    # Start with activity options if provided, otherwise empty temporal config
    if activity_options:
        result = activity_options.copy()
        # Ensure temporal key exists
        if "temporal" not in result:
            result["temporal"] = {}
    else:
        result = {"temporal": {}}

    # Add run_in_workflow flag if True
    if run_in_workflow:
        result["temporal"]["run_in_workflow"] = True

    return result


def compile(
    graph_id: str,
    *,
    default_activity_options: Optional[dict[str, Any]] = None,
    per_node_activity_options: Optional[dict[str, dict[str, Any]]] = None,
    enable_workflow_execution: bool = False,
    checkpoint: Optional[dict] = None,
) -> TemporalLangGraphRunner:
    """Compile a registered LangGraph graph for Temporal execution.

    This function retrieves a graph from the plugin registry and wraps it
    in a TemporalLangGraphRunner for durable execution within workflows.

    The graph must be registered with LangGraphPlugin before calling this
    function. Registration happens when the plugin is created:

        plugin = LangGraphPlugin(graphs={"my_graph": build_my_graph})

    Activity options can be set at multiple levels with the following priority
    (highest to lowest):
    1. Node metadata from `add_node(metadata=...)`
    2. `per_node_activity_options` from `compile()`
    3. `per_node_activity_options` from `LangGraphPlugin()`
    4. `default_activity_options` from `compile()`
    5. `default_activity_options` from `LangGraphPlugin()`
    6. Built-in defaults (5 min timeout, 3 retries)

    Args:
        graph_id: ID of the graph registered with LangGraphPlugin.
            This should match a key in the `graphs` dict passed to the plugin.
        default_activity_options: Default activity options for all nodes, created
            via `node_activity_options()`. Overrides plugin-level defaults.
            Node-specific options override these.
        per_node_activity_options: Per-node options mapping node names to
            `node_activity_options()`. Overrides plugin-level per-node options.
            Use this to configure existing graphs without modifying their source
            code. Node metadata from `add_node(metadata=...)` takes precedence.
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
            >>> from temporalio.contrib.langgraph import LangGraphPlugin, node_activity_options
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

        Usage with defaults (workflow.py):
            >>> from temporalio.contrib.langgraph import compile, node_activity_options
            >>>
            >>> @workflow.defn
            >>> class WeatherAgentWorkflow:
            ...     @workflow.run
            ...     async def run(self, graph_id: str, query: str):
            ...         app = compile(
            ...             graph_id,
            ...             default_activity_options=node_activity_options(
            ...                 start_to_close_timeout=timedelta(minutes=10),
            ...             ),
            ...         )
            ...         return await app.ainvoke({"query": query})

        Usage with per-node options (existing graphs):
            >>> app = compile(
            ...     "my_graph",
            ...     default_activity_options=node_activity_options(
            ...         start_to_close_timeout=timedelta(minutes=5),
            ...     ),
            ...     per_node_activity_options={
            ...         "slow_node": node_activity_options(
            ...             start_to_close_timeout=timedelta(hours=2),
            ...         ),
            ...         "gpu_node": node_activity_options(
            ...             task_queue="gpu-workers",
            ...             start_to_close_timeout=timedelta(hours=1),
            ...         ),
            ...     },
            ... )

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

    # Get plugin-level options from registry
    plugin_default_options = get_default_activity_options(graph_id)
    plugin_per_node_options = get_per_node_activity_options(graph_id)

    def _merge_activity_options(
        base: dict[str, Any], override: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge activity options, with override taking precedence.

        Both dicts have structure {"temporal": {...}} from node_activity_options().
        We need to merge the inner "temporal" dicts.
        """
        base_temporal = base.get("temporal", {})
        override_temporal = override.get("temporal", {})
        return {"temporal": {**base_temporal, **override_temporal}}

    # Merge options: compile options override plugin options
    merged_default_options: Optional[dict[str, Any]] = None
    if plugin_default_options or default_activity_options:
        merged_default_options = _merge_activity_options(
            plugin_default_options or {}, default_activity_options or {}
        )

    merged_per_node_options: Optional[dict[str, dict[str, Any]]] = None
    if plugin_per_node_options or per_node_activity_options:
        merged_per_node_options = {}
        # Start with plugin options
        for node_name, node_opts in (plugin_per_node_options or {}).items():
            merged_per_node_options[node_name] = node_opts
        # Merge compile options
        if per_node_activity_options:
            for node_name, node_opts in per_node_activity_options.items():
                if node_name in merged_per_node_options:
                    merged_per_node_options[node_name] = _merge_activity_options(
                        merged_per_node_options[node_name], node_opts
                    )
                else:
                    merged_per_node_options[node_name] = node_opts

    return TemporalLangGraphRunner(
        pregel,
        graph_id=graph_id,
        default_activity_options=merged_default_options,
        per_node_activity_options=merged_per_node_options,
        enable_workflow_execution=enable_workflow_execution,
        checkpoint=checkpoint,
    )


__all__ = [
    "compile",
    "LangGraphPlugin",
    "node_activity_options",
    "register_model",
    "register_model_factory",
    "register_tool",
    "StateSnapshot",
    "temporal_model",
    "temporal_node_metadata",
    "temporal_tool",
    "TemporalLangGraphRunner",
]
