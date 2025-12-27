"""LangGraph plugin for Temporal integration.

This module provides the LangGraphPlugin class which handles:
- Graph builder registration
- Activity auto-registration
- Data converter configuration
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio.contrib.langgraph._graph_registry import (
    register_graph,
)
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


def _langgraph_data_converter(converter: DataConverter | None) -> DataConverter:
    """Configure data converter for LangGraph serialization.

    Uses PydanticPayloadConverter to handle LangChain message serialization.

    Args:
        converter: The existing data converter, if any.

    Returns:
        A DataConverter configured for LangGraph.
    """
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


class LangGraphPlugin(SimplePlugin):
    """Temporal plugin for LangGraph integration.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin provides seamless integration between LangGraph and Temporal:

    1. **Graph Registration**: Register graph builders by ID for lookup during execution
    2. **Activity Auto-Registration**: Node execution activities are automatically registered
    3. **Data Converter**: Configures Pydantic converter for LangChain message serialization
    4. **Graph Caching**: Compiled graphs are cached per worker process (thread-safe)

    Example:
        >>> from temporalio.client import Client
        >>> from temporalio.worker import Worker
        >>> from temporalio.contrib.langgraph import LangGraphPlugin
        >>> from langgraph.graph import StateGraph
        >>>
        >>> # Define graph builders at module level
        >>> def build_weather_agent():
        ...     graph = StateGraph(AgentState)
        ...     graph.add_node("fetch", fetch_weather)
        ...     graph.add_node("process", process_data)
        ...     # ... add edges ...
        ...     return graph.compile()
        >>>
        >>> # Create plugin with registered graphs
        >>> plugin = LangGraphPlugin(
        ...     graphs={
        ...         "weather_agent": build_weather_agent,
        ...     },
        ...     default_activity_timeout=timedelta(minutes=5),
        ... )
        >>>
        >>> # Use with client - activities auto-registered
        >>> client = await Client.connect("localhost:7233", plugins=[plugin])
        >>> worker = Worker(
        ...     client,
        ...     task_queue="langgraph-workers",
        ...     workflows=[WeatherAgentWorkflow],
        ... )
    """

    def __init__(
        self,
        graphs: dict[str, Callable[[], Pregel]],
        default_activity_timeout: timedelta = timedelta(minutes=5),
        default_max_retries: int = 3,
        default_activity_options: dict[str, Any] | None = None,
        per_node_activity_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the LangGraph plugin.

        Args:
            graphs: Mapping of graph_id to builder function.
                Builder functions should return a compiled Pregel graph.
                Example: {"my_agent": build_my_agent}
            default_activity_timeout: Default timeout for node activities.
                Can be overridden per-node via metadata.
            default_max_retries: Default retry attempts for node activities.
            default_activity_options: Default activity options for all nodes across
                all graphs. Created via `node_activity_options()`. These are used
                as base defaults that can be overridden by `compile()` or node metadata.
            per_node_activity_options: Per-node activity options mapping node names
                to options dicts. Created via `node_activity_options()`. These apply
                to nodes across all graphs and can be overridden by `compile()` or
                node metadata.

        Raises:
            ValueError: If duplicate graph IDs are provided.
        """
        self._graphs = graphs
        self.default_activity_timeout = default_activity_timeout
        self.default_max_retries = default_max_retries
        self._default_activity_options = default_activity_options
        self._per_node_activity_options = per_node_activity_options

        # Register graphs in global registry with activity options
        for graph_id, builder in graphs.items():
            register_graph(
                graph_id,
                builder,
                default_activity_options=default_activity_options,
                per_node_activity_options=per_node_activity_options,
            )

        def add_activities(
            activities: Sequence[Callable[..., Any]] | None,
        ) -> Sequence[Callable[..., Any]]:
            """Add LangGraph activities for node, tool, and model execution."""
            from temporalio.contrib.langgraph._activities import (
                execute_chat_model,
                execute_node,
                execute_tool,
            )

            return list(activities or []) + [
                execute_node,
                execute_tool,
                execute_chat_model,
            ]

        super().__init__(
            name="LangGraphPlugin",
            data_converter=_langgraph_data_converter,
            activities=add_activities,
        )

    def get_graph_ids(self) -> list[str]:
        """Get list of registered graph IDs.

        Returns:
            List of graph IDs registered with this plugin.
        """
        return list(self._graphs.keys())

    def is_graph_registered(self, graph_id: str) -> bool:
        """Check if a graph is registered.

        Args:
            graph_id: The ID to check.

        Returns:
            True if the graph is registered, False otherwise.
        """
        return graph_id in self._graphs
