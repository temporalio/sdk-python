"""Unit tests for LangGraph registries.

Tests for GraphRegistry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph


class TestGraphRegistry:
    """Tests for the graph registry."""

    def test_register_and_get(self) -> None:
        """Registry should cache graph after first access."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        class State(TypedDict, total=False):
            value: int

        def build_graph():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        registry = GraphRegistry()
        registry.register("test_graph", build_graph)

        # First access builds
        graph1 = registry.get_graph("test_graph")
        assert graph1 is not None

        # Second access returns cached
        graph2 = registry.get_graph("test_graph")
        assert graph1 is graph2

    def test_get_nonexistent_raises(self) -> None:
        """Getting nonexistent graph should raise ApplicationError."""
        from temporalio.contrib.langgraph import GRAPH_NOT_FOUND_ERROR
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry
        from temporalio.exceptions import ApplicationError

        registry = GraphRegistry()

        with pytest.raises(ApplicationError) as exc_info:
            registry.get_graph("nonexistent")
        assert exc_info.value.type == GRAPH_NOT_FOUND_ERROR

    def test_register_duplicate_raises(self) -> None:
        """Registering duplicate graph ID should raise GraphAlreadyRegisteredError."""
        from temporalio.contrib.langgraph import GraphAlreadyRegisteredError
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("dup", lambda: MagicMock())

        with pytest.raises(GraphAlreadyRegisteredError):
            registry.register("dup", lambda: MagicMock())

    def test_get_node(self) -> None:
        """Registry should allow getting specific nodes."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        class State(TypedDict, total=False):
            value: int

        def my_node(state: State) -> State:
            return {"value": state.get("value", 0) + 1}

        def build_graph():
            graph = StateGraph(State)
            graph.add_node("my_node", my_node)
            graph.add_edge(START, "my_node")
            graph.add_edge("my_node", END)
            return graph.compile()

        registry = GraphRegistry()
        registry.register("test_graph", build_graph)

        node = registry.get_node("test_graph", "my_node")
        assert node is not None

    def test_list_graphs(self) -> None:
        """Registry should list registered graph IDs."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("graph_a", lambda: MagicMock())
        registry.register("graph_b", lambda: MagicMock())

        graphs = registry.list_graphs()
        assert "graph_a" in graphs
        assert "graph_b" in graphs

    def test_clear(self) -> None:
        """Registry clear should remove all entries."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("graph", lambda: MagicMock())
        registry.clear()

        assert not registry.is_registered("graph")


