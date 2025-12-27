"""Thread-safe graph registry for LangGraph-Temporal integration."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from temporalio.contrib.langgraph._exceptions import (
    GraphAlreadyRegisteredError,
    graph_not_found_error,
    node_not_found_error,
)

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


class GraphRegistry:
    """Thread-safe registry for graph builders and cached compiled graphs.

    Graphs are built once per worker process and cached. Uses double-checked
    locking for thread-safe access.
    """

    def __init__(self) -> None:
        self._builders: dict[str, Callable[[], Pregel]] = {}
        self._cache: dict[str, Pregel] = {}
        self._default_activity_options: dict[str, dict[str, Any]] = {}
        self._per_node_activity_options: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def register(
        self,
        graph_id: str,
        builder: Callable[[], Pregel],
        default_activity_options: dict[str, Any] | None = None,
        per_node_activity_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Register a graph builder by ID. Builds immediately for sandbox safety."""
        with self._lock:
            if graph_id in self._builders:
                raise GraphAlreadyRegisteredError(graph_id)
            self._builders[graph_id] = builder
            # Eagerly build the graph to ensure compilation happens outside
            # the workflow sandbox where all Python types are available
            self._cache[graph_id] = builder()
            if default_activity_options:
                self._default_activity_options[graph_id] = default_activity_options
            if per_node_activity_options:
                self._per_node_activity_options[graph_id] = per_node_activity_options

    def get_graph(self, graph_id: str) -> Pregel:
        """Get a compiled graph by ID, building and caching if needed."""
        # Fast path: check cache without lock (dict read is atomic in CPython)
        if graph_id in self._cache:
            return self._cache[graph_id]

        # Slow path: acquire lock and build if needed
        with self._lock:
            # Double-check after acquiring lock
            if graph_id in self._cache:
                return self._cache[graph_id]

            if graph_id not in self._builders:
                available = list(self._builders.keys())
                raise graph_not_found_error(graph_id, available)

            # Build and cache
            builder = self._builders[graph_id]
            graph = builder()
            self._cache[graph_id] = graph
            return graph

    def get_node(self, graph_id: str, node_name: str) -> Any:
        """Get a specific node's runnable from a cached graph."""
        graph = self.get_graph(graph_id)

        if node_name not in graph.nodes:
            available = list(graph.nodes.keys())
            raise node_not_found_error(node_name, graph_id, available)

        return graph.nodes[node_name]

    def list_graphs(self) -> list[str]:
        """List all registered graph IDs."""
        with self._lock:
            return list(self._builders.keys())

    def is_registered(self, graph_id: str) -> bool:
        """Check if a graph is registered."""
        with self._lock:
            return graph_id in self._builders

    def get_default_activity_options(self, graph_id: str) -> dict[str, Any]:
        """Get default activity options for a graph."""
        return self._default_activity_options.get(graph_id, {})

    def get_per_node_activity_options(self, graph_id: str) -> dict[str, dict[str, Any]]:
        """Get per-node activity options for a graph."""
        return self._per_node_activity_options.get(graph_id, {})

    def clear(self) -> None:
        """Clear all registered entries. Mainly for testing."""
        with self._lock:
            self._builders.clear()
            self._cache.clear()
            self._default_activity_options.clear()
            self._per_node_activity_options.clear()


# Global registry instance
_global_registry = GraphRegistry()


def get_global_registry() -> GraphRegistry:
    """Get the global graph registry instance."""
    return _global_registry


def register_graph(
    graph_id: str,
    builder: Callable[[], Pregel],
    default_activity_options: dict[str, Any] | None = None,
    per_node_activity_options: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Register a graph builder in the global registry."""
    _global_registry.register(
        graph_id, builder, default_activity_options, per_node_activity_options
    )


def get_graph(graph_id: str) -> Pregel:
    """Get a compiled graph from the global registry."""
    return _global_registry.get_graph(graph_id)


def get_node(graph_id: str, node_name: str) -> Any:
    """Get a node from a graph in the global registry."""
    return _global_registry.get_node(graph_id, node_name)


def get_default_activity_options(graph_id: str) -> dict[str, Any]:
    """Get default activity options for a graph from the global registry."""
    return _global_registry.get_default_activity_options(graph_id)


def get_per_node_activity_options(graph_id: str) -> dict[str, dict[str, Any]]:
    """Get per-node activity options for a graph from the global registry."""
    return _global_registry.get_per_node_activity_options(graph_id)
