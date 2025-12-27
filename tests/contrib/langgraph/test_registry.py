"""Unit tests for LangGraph registries.

Tests for GraphRegistry, tool registry, and model registry.
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


class TestToolRegistry:
    """Tests for the tool registry."""

    def test_register_and_get_tool(self) -> None:
        """Should register and retrieve tools by name."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            get_tool,
            register_tool,
        )

        @tool
        def my_tool(query: str) -> str:
            """A test tool."""
            return f"Result: {query}"

        register_tool(my_tool)

        retrieved = get_tool("my_tool")
        assert retrieved is my_tool

    def test_get_nonexistent_tool_raises(self) -> None:
        """Should raise ApplicationError for unregistered tools."""
        from temporalio.contrib.langgraph import TOOL_NOT_FOUND_ERROR
        from temporalio.contrib.langgraph._tool_registry import get_tool
        from temporalio.exceptions import ApplicationError

        with pytest.raises(ApplicationError) as exc_info:
            get_tool("nonexistent_tool")
        assert exc_info.value.type == TOOL_NOT_FOUND_ERROR

    def test_register_duplicate_tool_same_instance(self) -> None:
        """Should allow re-registering the same tool instance."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            get_tool,
            register_tool,
        )

        @tool
        def my_tool(query: str) -> str:
            """A test tool."""
            return query

        register_tool(my_tool)
        register_tool(my_tool)  # Same instance, should not raise

        assert get_tool("my_tool") is my_tool

    def test_get_all_tools(self) -> None:
        """Should return all registered tools."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            get_all_tools,
            register_tool,
        )

        @tool
        def tool_a(x: str) -> str:
            """Tool A."""
            return x

        @tool
        def tool_b(x: str) -> str:
            """Tool B."""
            return x

        register_tool(tool_a)
        register_tool(tool_b)

        all_tools = get_all_tools()
        assert "tool_a" in all_tools
        assert "tool_b" in all_tools


class TestModelRegistry:
    """Tests for the model registry."""

    def test_register_and_get_model(self) -> None:
        """Should register and retrieve models by name."""
        from temporalio.contrib.langgraph._model_registry import (
            get_model,
            register_model,
        )

        # Create a mock model
        mock_model = MagicMock()
        mock_model.model_name = "test-model"

        register_model(mock_model)

        retrieved = get_model("test-model")
        assert retrieved is mock_model

    def test_register_model_with_explicit_name(self) -> None:
        """Should register model with explicit name."""
        from temporalio.contrib.langgraph._model_registry import (
            get_model,
            register_model,
        )

        mock_model = MagicMock()
        register_model(mock_model, name="custom-name")

        retrieved = get_model("custom-name")
        assert retrieved is mock_model

    def test_get_nonexistent_model_raises(self) -> None:
        """Should raise ApplicationError for unregistered models."""
        from temporalio.contrib.langgraph import MODEL_NOT_FOUND_ERROR
        from temporalio.contrib.langgraph._model_registry import get_model
        from temporalio.exceptions import ApplicationError

        with pytest.raises(ApplicationError) as exc_info:
            get_model("nonexistent-model")
        assert exc_info.value.type == MODEL_NOT_FOUND_ERROR

    def test_register_model_factory(self) -> None:
        """Should support lazy model instantiation via factory."""
        from temporalio.contrib.langgraph._model_registry import (
            get_model,
            register_model_factory,
        )

        mock_model = MagicMock()
        factory_called = False

        def model_factory():
            nonlocal factory_called
            factory_called = True
            return mock_model

        register_model_factory("lazy-model", model_factory)

        # Factory not called yet
        assert factory_called is False

        # Get model - factory should be called
        retrieved = get_model("lazy-model")
        assert factory_called is True
        assert retrieved is mock_model

        # Second get should use cached instance
        factory_called = False
        retrieved2 = get_model("lazy-model")
        assert factory_called is False
        assert retrieved2 is mock_model
