"""LangGraph plugin for Temporal integration."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio.contrib.langgraph._graph_registry import (
    register_graph,
)
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


def _langgraph_data_converter(converter: DataConverter | None) -> DataConverter:
    """Configure data converter with PydanticPayloadConverter for LangChain messages."""
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

    Registers graph builders, auto-registers node execution activities,
    and configures the Pydantic data converter for LangChain messages.
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
            default_activity_timeout: Default timeout for node activities.
            default_max_retries: Default retry attempts for node activities.
            default_activity_options: Default options for all nodes.
            per_node_activity_options: Per-node options by node name.
        """
        self._graphs = graphs
        self.default_activity_timeout = default_activity_timeout
        self.default_max_retries = default_max_retries
        self._default_activity_options = default_activity_options
        self._per_node_activity_options = per_node_activity_options

        logger.debug(
            "Initializing LangGraphPlugin with %d graphs: %s",
            len(graphs),
            list(graphs.keys()),
        )

        # Register graphs in global registry with activity options
        for graph_id, builder in graphs.items():
            register_graph(
                graph_id,
                builder,
                default_activity_options=default_activity_options,
                per_node_activity_options=per_node_activity_options,
            )
            logger.debug("Registered graph: %s", graph_id)

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
        """Get list of registered graph IDs."""
        return list(self._graphs.keys())

    def is_graph_registered(self, graph_id: str) -> bool:
        """Check if a graph is registered."""
        return graph_id in self._graphs
