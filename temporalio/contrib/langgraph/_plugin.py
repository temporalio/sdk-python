"""Unified LangGraph plugin for Temporal integration."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from temporalio.contrib.langgraph._functional_activity import execute_langgraph_task
from temporalio.contrib.langgraph._functional_registry import register_entrypoint
from temporalio.contrib.langgraph._graph_registry import register_graph
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langgraph.pregel import Pregel

# Type alias for activity options keys
# Can be:
#   - str: "node_or_task_name" (global - applies to all graphs/entrypoints)
#   - tuple: ("graph_or_entrypoint_id", "node_or_task_name") (scoped)
ActivityOptionsKey = str | tuple[str, str]


def _langgraph_data_converter(converter: DataConverter | None) -> DataConverter:
    """Configure data converter with PydanticPayloadConverter for LangChain messages."""
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


def _is_entrypoint(value: Any) -> bool:
    """Check if a value is an @entrypoint decorated function (Pregel).

    @entrypoint creates a Pregel (not CompiledStateGraph) with a single node
    named after the function (not __start__).
    """
    try:
        from langgraph.graph.state import CompiledStateGraph
        from langgraph.pregel import Pregel

        if not isinstance(value, Pregel):
            return False

        # CompiledStateGraph (from StateGraph.compile()) is NOT an entrypoint
        # @entrypoint returns a plain Pregel, not a CompiledStateGraph subclass
        if isinstance(value, CompiledStateGraph):
            return False

        # @entrypoint creates a single-node Pregel without __start__ node
        # StateGraph.compile() always has __start__ + user nodes
        if "__start__" in value.nodes:
            return False

        # Additional check: should have exactly one node (the entrypoint function)
        if len(value.nodes) != 1:
            return False

        return True

    except ImportError:
        return False


def _is_compiled_graph(value: Any) -> bool:
    """Check if a value is a compiled StateGraph (Pregel) but not an entrypoint."""
    try:
        from langgraph.pregel import Pregel

        if isinstance(value, Pregel):
            # It's a Pregel, but not an entrypoint
            return not _is_entrypoint(value)
        return False
    except ImportError:
        return False


def _make_graph_builder(graph: Pregel) -> Callable[[], Pregel]:
    """Create a builder function that returns the given graph."""

    def builder() -> Pregel:
        return graph

    return builder


class LangGraphPlugin(SimplePlugin):
    """Unified Temporal plugin for LangGraph integration.

    Supports both Graph API (StateGraph) and Functional API (@entrypoint/@task).

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Example::

        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        plugin = LangGraphPlugin(
            graphs={
                # Graph API (CompiledGraph or Callable returning one)
                "my_graph": graph.compile(),
                "lazy_graph": build_graph,
                # Functional API (@entrypoint decorated functions)
                "my_entrypoint": entrypoint_func,
            },
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(minutes=5),
            ),
            activity_options={
                # Global: applies to any graph/entrypoint with this node/task
                "call_model": activity_options(
                    start_to_close_timeout=timedelta(minutes=2)
                ),
                # Scoped to specific graph or entrypoint
                ("my_graph", "expensive_node"): activity_options(
                    start_to_close_timeout=timedelta(minutes=10),
                ),
            },
        )

    Activity options are resolved in priority order (highest to lowest):
    scoped key, global key, default_activity_options, then fallback (10 min).
    """

    def __init__(
        self,
        graphs: dict[str, Pregel | Callable[[], Pregel]] | None = None,
        default_activity_options: dict[str, Any] | None = None,
        activity_options: dict[ActivityOptionsKey, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the unified LangGraph plugin.

        Args:
            graphs: Mapping of ID to graph/entrypoint (CompiledGraph, callable,
                or @entrypoint function).
            default_activity_options: Default options for all nodes/tasks.
            activity_options: Per-node/task options keyed by name or tuple.
        """
        self._graphs = graphs or {}
        self._default_activity_options = default_activity_options
        self._activity_options = activity_options or {}

        # Track which IDs are graphs vs entrypoints
        self._graph_ids: set[str] = set()
        self._entrypoint_ids: set[str] = set()

        logger.debug(
            "Initializing LangGraphPlugin with %d registrations",
            len(self._graphs),
        )

        # Process and register each entry
        for entry_id, value in self._graphs.items():
            self._register_entry(entry_id, value)

        # Setup activities based on what was registered
        def add_activities(
            activities: Sequence[Callable[..., Any]] | None,
        ) -> Sequence[Callable[..., Any]]:
            """Add LangGraph activities for node and task execution."""
            result = list(activities or [])

            # Add graph API activities if any graphs registered
            if self._graph_ids:
                from temporalio.contrib.langgraph._activities import (
                    langgraph_node,
                    langgraph_tool_node,
                    resume_langgraph_node,
                )

                result.extend(
                    [langgraph_node, langgraph_tool_node, resume_langgraph_node]
                )

            # Add functional API activity if any entrypoints registered
            if self._entrypoint_ids:
                result.append(execute_langgraph_task)

            return result

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            """Configure sandbox passthrough for LangGraph dependencies."""
            if not runner:
                raise ValueError("No WorkflowRunner provided to LangGraphPlugin.")

            if isinstance(runner, SandboxedWorkflowRunner):
                passthrough_modules = [
                    "pydantic_core",
                    "langchain_core",
                    "annotated_types",
                ]

                # Add additional modules for functional API
                if self._entrypoint_ids:
                    passthrough_modules.extend(["langgraph", "langsmith"])

                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        *passthrough_modules
                    ),
                )
            return runner

        super().__init__(
            name="LangGraphPlugin",
            data_converter=_langgraph_data_converter,
            activities=add_activities,
            workflow_runner=workflow_runner,
        )

    def _register_entry(self, entry_id: str, value: Any) -> None:
        """Register a single graph or entrypoint entry."""
        # Get activity options for this entry
        default_opts = self._default_activity_options
        per_item_opts = self._get_per_item_options(entry_id)

        # Check if it's already a Pregel (compiled graph or entrypoint)
        if _is_entrypoint(value):
            self._register_as_entrypoint(entry_id, value, default_opts, per_item_opts)
        elif _is_compiled_graph(value):
            self._register_as_graph(
                entry_id, _make_graph_builder(value), default_opts, per_item_opts
            )
        elif callable(value):
            # It's a callable - could be lazy graph builder or entrypoint
            # Try to call it and detect
            try:
                result = value()
                if _is_entrypoint(result):
                    self._register_as_entrypoint(
                        entry_id,
                        cast("Pregel", result),
                        default_opts,
                        per_item_opts,
                    )
                elif _is_compiled_graph(result):
                    self._register_as_graph(
                        entry_id,
                        _make_graph_builder(cast("Pregel", result)),
                        default_opts,
                        per_item_opts,
                    )
                else:
                    raise ValueError(
                        f"Callable '{entry_id}' returned {type(result)}, "
                        "expected CompiledGraph or @entrypoint"
                    )
            except Exception as e:
                raise ValueError(
                    f"Failed to process '{entry_id}': {e}. "
                    "Expected CompiledGraph, @entrypoint, or callable returning one."
                ) from e
        else:
            raise ValueError(
                f"Unknown type for '{entry_id}': {type(value)}. "
                "Expected CompiledGraph, @entrypoint, or callable."
            )

    def _get_per_item_options(self, entry_id: str) -> dict[str, dict[str, Any]]:
        """Extract per-node/task options for a specific graph/entrypoint."""
        result: dict[str, dict[str, Any]] = {}

        for key, opts in self._activity_options.items():
            if isinstance(key, tuple):
                # Scoped option: ("entry_id", "node_or_task_name")
                if key[0] == entry_id:
                    result[key[1]] = opts
            else:
                # Global option: applies to all
                result[key] = opts

        return result

    def _register_as_graph(
        self,
        graph_id: str,
        builder: Callable[[], Pregel],
        default_opts: dict[str, Any] | None,
        per_node_opts: dict[str, dict[str, Any]],
    ) -> None:
        """Register as a Graph API graph."""
        register_graph(
            graph_id,
            builder,
            default_activity_options=default_opts,
            per_node_activity_options=per_node_opts,
        )
        self._graph_ids.add(graph_id)
        logger.debug("Registered graph: %s", graph_id)

    def _register_as_entrypoint(
        self,
        entrypoint_id: str,
        entrypoint: Pregel,
        default_opts: dict[str, Any] | None,
        per_task_opts: dict[str, dict[str, Any]],
    ) -> None:
        """Register as a Functional API entrypoint."""
        # Convert default_opts to task options format
        default_task_opts = {}
        if default_opts:
            # Unwrap activity_options() format if needed
            if "temporal" in default_opts:
                default_task_opts = default_opts["temporal"]
            else:
                default_task_opts = default_opts

        register_entrypoint(
            entrypoint_id,
            entrypoint,
            default_task_options=default_task_opts,
            per_task_options=per_task_opts,
        )
        self._entrypoint_ids.add(entrypoint_id)
        logger.debug("Registered entrypoint: %s", entrypoint_id)

    def get_graph_ids(self) -> list[str]:
        """Get list of registered graph IDs (Graph API)."""
        return list(self._graph_ids)

    def get_entrypoint_ids(self) -> list[str]:
        """Get list of registered entrypoint IDs (Functional API)."""
        return list(self._entrypoint_ids)

    def get_all_ids(self) -> list[str]:
        """Get list of all registered IDs (both graphs and entrypoints)."""
        return list(self._graphs.keys())

    def is_graph(self, entry_id: str) -> bool:
        """Check if an ID refers to a Graph API graph."""
        return entry_id in self._graph_ids

    def is_entrypoint(self, entry_id: str) -> bool:
        """Check if an ID refers to a Functional API entrypoint."""
        return entry_id in self._entrypoint_ids

    def is_registered(self, entry_id: str) -> bool:
        """Check if an ID is registered (either graph or entrypoint)."""
        return entry_id in self._graphs
