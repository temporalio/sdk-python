"""Temporal activities for LangGraph node execution.

This module provides the activity that executes LangGraph nodes within
Temporal workflows. The activity retrieves the graph from the registry,
looks up the node, executes it, and captures the writes.
"""

from __future__ import annotations

import asyncio
import warnings
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from temporalio import activity

from temporalio.contrib.langgraph._graph_registry import get_graph
from temporalio.contrib.langgraph._models import (
    ChannelWrite,
    NodeActivityInput,
    NodeActivityOutput,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from temporalio.contrib.langgraph._plugin import LangGraphPlugin

# Import CONFIG_KEY_SEND for write capture injection
# This is deprecated in LangGraph v1.0 but still required for node execution
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from langgraph.constants import CONFIG_KEY_SEND


class NodeExecutionActivity:
    """Activity class for executing LangGraph nodes.

    This activity:
    1. Retrieves the cached graph from the registry
    2. Looks up the node by name
    3. Executes the node with the provided state
    4. Captures writes via CONFIG_KEY_SEND callback
    5. Returns writes wrapped in ChannelWrite for type preservation

    The activity uses heartbeats to report progress during execution.
    """

    def __init__(self, plugin: LangGraphPlugin) -> None:
        """Initialize the activity with a reference to the plugin.

        Args:
            plugin: The LangGraphPlugin instance for configuration access.
        """
        self._plugin = plugin

    @activity.defn(name="execute_langgraph_node")
    async def execute_node(self, input_data: NodeActivityInput) -> NodeActivityOutput:
        """Execute a LangGraph node as a Temporal activity.

        Args:
            input_data: The input data containing node name, graph ID, state, etc.

        Returns:
            NodeActivityOutput containing the writes produced by the node.

        Raises:
            ValueError: If the node is not found in the graph.
            Exception: Any exception raised by the node during execution.
        """
        # Get cached graph from registry
        graph = get_graph(input_data.graph_id)

        # Get node
        pregel_node = graph.nodes.get(input_data.node_name)
        if pregel_node is None:
            available = list(graph.nodes.keys())
            raise ValueError(
                f"Node '{input_data.node_name}' not found in graph "
                f"'{input_data.graph_id}'. Available nodes: {available}"
            )

        # Get the node's runnable
        node_runnable = pregel_node.node
        if node_runnable is None:
            return NodeActivityOutput(writes=[])

        # Setup write capture deque
        # Writers in LangGraph call CONFIG_KEY_SEND callback with list of (channel, value) tuples
        writes: deque[tuple[str, Any]] = deque()

        # Build config with write callback injected
        # CONFIG_KEY_SEND is REQUIRED - nodes with writers will fail without it
        # The callback receives a list of (channel, value) tuples from ChannelWrite operations
        config: dict[str, Any] = {
            **input_data.config,
            "configurable": {
                **input_data.config.get("configurable", {}),
                CONFIG_KEY_SEND: writes.extend,  # Callback to capture writes
            },
        }

        # Send heartbeat indicating execution start
        activity.heartbeat(
            {
                "node": input_data.node_name,
                "task_id": input_data.task_id,
                "graph_id": input_data.graph_id,
                "status": "executing",
            }
        )

        # Execute the node
        # The node_runnable includes the bound function and writers
        # Cast config to RunnableConfig for type checking
        runnable_config = cast("RunnableConfig", config)
        try:
            if asyncio.iscoroutinefunction(
                getattr(node_runnable, "ainvoke", None)
            ) or asyncio.iscoroutinefunction(getattr(node_runnable, "invoke", None)):
                result = await node_runnable.ainvoke(
                    input_data.input_state, runnable_config
                )
            else:
                result = node_runnable.invoke(input_data.input_state, runnable_config)
        except Exception:
            # Send heartbeat indicating failure before re-raising
            activity.heartbeat(
                {
                    "node": input_data.node_name,
                    "task_id": input_data.task_id,
                    "graph_id": input_data.graph_id,
                    "status": "failed",
                }
            )
            raise

        # Note: Writes are primarily captured via CONFIG_KEY_SEND callback above.
        # The callback is invoked by LangGraph's internal writer mechanism.
        # For nodes that return dicts directly (without using writers),
        # we also check the result as a fallback.
        if isinstance(result, dict) and not writes:
            # Only use result if CONFIG_KEY_SEND didn't capture anything
            for channel, value in result.items():
                writes.append((channel, value))

        # Send heartbeat indicating completion
        activity.heartbeat(
            {
                "node": input_data.node_name,
                "task_id": input_data.task_id,
                "graph_id": input_data.graph_id,
                "status": "completed",
                "writes_count": len(writes),
            }
        )

        # Convert writes to ChannelWrite for type preservation
        channel_writes = [
            ChannelWrite.create(channel, value) for channel, value in writes
        ]

        return NodeActivityOutput(writes=channel_writes)
