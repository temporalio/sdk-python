"""Temporal integration for LangGraph.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Union

import temporalio.common
import temporalio.workflow
from temporalio.contrib.langgraph._constants import (
    CHECKPOINT_KEY,
    INTERRUPT_KEY,
)
from temporalio.contrib.langgraph._exceptions import (
    GRAPH_DEFINITION_CHANGED_ERROR,
    GRAPH_NOT_FOUND_ERROR,
    NODE_NOT_FOUND_ERROR,
    GraphAlreadyRegisteredError,
)
from temporalio.contrib.langgraph._functional_activity import execute_langgraph_task
from temporalio.contrib.langgraph._functional_registry import (
    get_entrypoint,
    register_entrypoint,
)
from temporalio.contrib.langgraph._functional_registry import (
    get_global_entrypoint_registry as _get_functional_registry,
)
from temporalio.contrib.langgraph._functional_runner import (
    TemporalFunctionalRunner,
)
from temporalio.contrib.langgraph._graph_registry import (
    get_default_activity_options,
    get_graph,
    get_per_node_activity_options,
    register_graph,
)
from temporalio.contrib.langgraph._graph_registry import (
    get_global_registry as _get_graph_registry,
)
from temporalio.contrib.langgraph._models import StateSnapshot
from temporalio.contrib.langgraph._plugin import LangGraphPlugin
from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

if TYPE_CHECKING:
    from temporalio.contrib.langgraph._plugin import ActivityOptionsKey


def activity_options(
    *,
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    start_to_close_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    task_queue: str | None = None,
    retry_policy: temporalio.common.RetryPolicy | None = None,
    cancellation_type: temporalio.workflow.ActivityCancellationType | None = None,
    versioning_intent: temporalio.workflow.VersioningIntent | None = None,
    summary: str | None = None,
    priority: temporalio.common.Priority | None = None,
) -> dict[str, Any]:
    """Create activity options for LangGraph integration.

    Use with plugin registration or compile() for workflow-level overrides.
    Parameters mirror ``workflow.execute_activity()``.

    .. warning::
        This API is experimental and may change in future versions.
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
    activity_options: dict[str, Any] | None = None,
    run_in_workflow: bool = False,
) -> dict[str, Any]:
    """Create node metadata combining activity options and execution flags.

    Args:
        activity_options: Options from ``activity_options()``.
        run_in_workflow: If True, run in workflow instead of as activity.

    .. warning::
        This API is experimental and may change in future versions.
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
    default_activity_options: dict[str, Any] | None = None,
    activity_options: dict[str, dict[str, Any]] | None = None,
    checkpoint: dict | None = None,
) -> Union[TemporalLangGraphRunner, TemporalFunctionalRunner]:
    """Compile a registered graph or entrypoint for Temporal execution.

    This function auto-detects whether the ID refers to a Graph API graph
    (StateGraph) or a Functional API entrypoint (@entrypoint/@task).

    .. warning::
        This API is experimental and may change in future versions.

    Args:
        graph_id: ID of graph or entrypoint registered with LangGraphPlugin.
        default_activity_options: Default options for all nodes/tasks.
            Use activity_options() helper to create.
        activity_options: Per-node/task options by name.
            Use activity_options() helper to create values.
        checkpoint: Checkpoint from previous get_state() for continue-as-new.
            Applies to both Graph API graphs and Functional API entrypoints.

    Returns:
        TemporalLangGraphRunner for Graph API graphs, or
        TemporalFunctionalRunner for Functional API entrypoints.

    Raises:
        ApplicationError: If no graph or entrypoint with the given ID is registered.
    """
    # Check which registry has this ID
    graph_registry = _get_graph_registry()
    functional_registry = _get_functional_registry()

    is_graph = graph_registry.is_registered(graph_id)
    is_entrypoint = functional_registry.is_registered(graph_id)

    if is_graph:
        return _compile_graph(
            graph_id,
            default_activity_options=default_activity_options,
            per_node_activity_options=activity_options,
            checkpoint=checkpoint,
        )
    elif is_entrypoint:
        return _compile_entrypoint(
            graph_id,
            default_activity_options=default_activity_options,
            task_options=activity_options,
            checkpoint=checkpoint,
        )
    else:
        # Neither registry has it - raise error
        from temporalio.exceptions import ApplicationError

        graph_ids = graph_registry.list_graphs()
        entrypoint_ids = functional_registry.list_entrypoints()
        all_ids = graph_ids + entrypoint_ids
        raise ApplicationError(
            f"'{graph_id}' not found. Available: {all_ids}",
            type=GRAPH_NOT_FOUND_ERROR,
            non_retryable=True,
        )


def _compile_graph(
    graph_id: str,
    *,
    default_activity_options: dict[str, Any] | None = None,
    per_node_activity_options: dict[str, dict[str, Any]] | None = None,
    checkpoint: dict | None = None,
) -> TemporalLangGraphRunner:
    """Compile a Graph API graph for Temporal execution."""
    # Get graph from registry
    pregel = get_graph(graph_id)

    # Get plugin-level options from registry
    plugin_default_options = get_default_activity_options(graph_id)
    plugin_per_node_options = get_per_node_activity_options(graph_id)

    def _merge_activity_options(
        base: dict[str, Any], override: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge activity options, with override taking precedence."""
        base_temporal = base.get("temporal", {})
        override_temporal = override.get("temporal", {})
        return {"temporal": {**base_temporal, **override_temporal}}

    # Merge options: compile options override plugin options
    merged_default_options: dict[str, Any] | None = None
    if plugin_default_options or default_activity_options:
        merged_default_options = _merge_activity_options(
            plugin_default_options or {}, default_activity_options or {}
        )

    merged_per_node_options: dict[str, dict[str, Any]] | None = None
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
        checkpoint=checkpoint,
    )


def _compile_entrypoint(
    entrypoint_id: str,
    *,
    default_activity_options: dict[str, Any] | None = None,
    task_options: dict[str, dict[str, Any]] | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> TemporalFunctionalRunner:
    """Compile a Functional API entrypoint for Temporal execution."""
    from temporalio.contrib.langgraph._functional_registry import (
        get_entrypoint_default_options,
        get_entrypoint_task_options,
    )

    # Get plugin-level options from registry
    plugin_default_options = get_entrypoint_default_options(entrypoint_id)
    plugin_task_options = get_entrypoint_task_options(entrypoint_id)

    # Merge default options
    merged_default_options: dict[str, Any] | None = None
    if plugin_default_options or default_activity_options:
        # Unwrap activity_options format if needed
        base = plugin_default_options or {}
        override = default_activity_options or {}
        if "temporal" in base:
            base = base.get("temporal", {})
        if "temporal" in override:
            override = override.get("temporal", {})
        merged_default_options = {**base, **override}

    # Merge per-task options
    merged_task_options: dict[str, dict[str, Any]] | None = None
    if plugin_task_options or task_options:
        merged_task_options = {}
        # Start with plugin options
        for task_name, opts in (plugin_task_options or {}).items():
            merged_task_options[task_name] = opts
        # Merge compile options
        if task_options:
            for task_name, opts in task_options.items():
                if task_name in merged_task_options:
                    # Merge the options
                    base = merged_task_options[task_name]
                    if "temporal" in base:
                        base = base.get("temporal", {})
                    override = opts
                    if "temporal" in override:
                        override = override.get("temporal", {})
                    merged_task_options[task_name] = {**base, **override}
                else:
                    # Unwrap if needed
                    if "temporal" in opts:
                        merged_task_options[task_name] = opts.get("temporal", {})
                    else:
                        merged_task_options[task_name] = opts

    # Get default timeout from merged options
    default_timeout = timedelta(minutes=5)
    if merged_default_options:
        if "start_to_close_timeout" in merged_default_options:
            default_timeout = merged_default_options["start_to_close_timeout"]

    return TemporalFunctionalRunner(
        entrypoint_id=entrypoint_id,
        default_task_timeout=default_timeout,
        task_options=merged_task_options,
        checkpoint=checkpoint,
    )


__all__ = [
    # Main unified API
    "activity_options",
    "compile",
    "LangGraphPlugin",
    "StateSnapshot",
    "temporal_node_metadata",
    # Runner types (for type annotations)
    "TemporalLangGraphRunner",
    "TemporalFunctionalRunner",
    # Registry functions (for direct registration outside LangGraphPlugin)
    "register_graph",
    "register_entrypoint",
    # Exception types (for catching configuration errors)
    "GraphAlreadyRegisteredError",
    # Error type constants (for catching ApplicationError.type)
    "GRAPH_NOT_FOUND_ERROR",
    "NODE_NOT_FOUND_ERROR",
    "GRAPH_DEFINITION_CHANGED_ERROR",
    # Constants for checking result state
    "CHECKPOINT_KEY",
    "INTERRUPT_KEY",
]
