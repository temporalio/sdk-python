"""Temporal integration for LangGraph.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import temporalio.common
import temporalio.workflow

from temporalio.contrib.langgraph._exceptions import (
    GRAPH_DEFINITION_CHANGED_ERROR,
    GRAPH_NOT_FOUND_ERROR,
    MODEL_NOT_FOUND_ERROR,
    NODE_NOT_FOUND_ERROR,
    TOOL_NOT_FOUND_ERROR,
    GraphAlreadyRegisteredError,
    ModelAlreadyRegisteredError,
    ToolAlreadyRegisteredError,
)
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

    Returns a dict for use with ``graph.add_node(metadata=...)`` or ``compile()``.
    Parameters mirror ``workflow.execute_activity()``.
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
    """Create node metadata combining activity options and execution flags.

    Args:
        activity_options: Options from ``node_activity_options()``.
        run_in_workflow: If True, run in workflow instead of as activity.
            Requires ``enable_workflow_execution=True`` on ``compile()``.
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
    """Compile a registered graph for Temporal execution.

    .. warning::
        This API is experimental and may change in future versions.

    Args:
        graph_id: ID of graph registered with LangGraphPlugin.
        default_activity_options: Default options for all nodes.
        per_node_activity_options: Per-node options by node name.
        enable_workflow_execution: Allow nodes to run in workflow.
        checkpoint: Checkpoint from previous get_state() for continue-as-new.

    Raises:
        ApplicationError: If no graph with the given ID is registered.
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
    # Main API
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
    # Exception types (for catching configuration errors)
    "GraphAlreadyRegisteredError",
    "ModelAlreadyRegisteredError",
    "ToolAlreadyRegisteredError",
    # Error type constants (for catching ApplicationError.type)
    "GRAPH_NOT_FOUND_ERROR",
    "NODE_NOT_FOUND_ERROR",
    "TOOL_NOT_FOUND_ERROR",
    "MODEL_NOT_FOUND_ERROR",
    "GRAPH_DEFINITION_CHANGED_ERROR",
]
