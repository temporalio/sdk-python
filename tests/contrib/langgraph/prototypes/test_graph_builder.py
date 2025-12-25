"""Tests for graph reconstruction mechanisms.

These tests validate the approaches for reconstructing graphs and accessing
node functions in Temporal activities.

Technical Concern:
    How do activities get access to node functions to execute them?
    The graph is built in the workflow, but activities run in a separate
    worker process.

Options Tested:
    1. Import function by module path
    2. Use a function registry
    3. Rebuild graph in activity and get node by name (recommended)
"""

from __future__ import annotations

from typing import Any

import pytest
from typing_extensions import TypedDict

from temporalio.contrib.langgraph._prototypes.graph_builder_proto import (
    FunctionRegistry,
    import_function,
    registry,
)


class TestImportFunction:
    """Test Option 1: Import function by module path."""

    def test_import_stdlib_function(self) -> None:
        """Import a function from standard library."""
        func = import_function("json.dumps")
        assert callable(func)
        result = func({"key": "value"})
        assert result == '{"key": "value"}'

    def test_import_stdlib_function_with_args(self) -> None:
        """Imported function should work with arguments."""
        func = import_function("json.dumps")
        result = func({"key": "value"}, indent=2)
        assert '"key": "value"' in result

    def test_import_nested_module(self) -> None:
        """Import from nested module."""
        func = import_function("os.path.join")
        assert callable(func)
        result = func("a", "b", "c")
        assert "a" in result and "b" in result and "c" in result

    def test_import_invalid_path_no_dot(self) -> None:
        """Should raise ImportError for path without dot."""
        with pytest.raises(ImportError, match="Invalid module path"):
            import_function("nodot")

    def test_import_nonexistent_module(self) -> None:
        """Should raise ImportError for nonexistent module."""
        with pytest.raises(ImportError):
            import_function("nonexistent_module_xyz.func")

    def test_import_nonexistent_function(self) -> None:
        """Should raise ImportError for nonexistent function."""
        with pytest.raises(ImportError, match="not found"):
            import_function("json.nonexistent_function")


class TestFunctionRegistry:
    """Test Option 2: Function registry."""

    def test_register_and_get(self) -> None:
        """Register a function and retrieve it."""
        test_registry = FunctionRegistry()

        @test_registry.register("test_func")
        def my_func(x: int) -> int:
            return x * 2

        retrieved = test_registry.get("test_func")
        assert retrieved is my_func
        assert retrieved(5) == 10

    def test_register_with_auto_name(self) -> None:
        """Register with automatic name from function."""
        test_registry = FunctionRegistry()

        @test_registry.register()
        def auto_named() -> str:
            return "auto"

        retrieved = test_registry.get("auto_named")
        assert retrieved is auto_named
        assert retrieved() == "auto"

    def test_get_nonexistent_raises(self) -> None:
        """Getting nonexistent function should raise KeyError."""
        test_registry = FunctionRegistry()

        with pytest.raises(KeyError, match="not found"):
            test_registry.get("nonexistent")

    def test_clear_registry(self) -> None:
        """Clear should remove all functions."""
        test_registry = FunctionRegistry()

        @test_registry.register("to_clear")
        def temp_func() -> None:
            pass

        test_registry.clear()

        with pytest.raises(KeyError):
            test_registry.get("to_clear")

    def test_singleton_instance(self) -> None:
        """get_instance should return same instance."""
        instance1 = FunctionRegistry.get_instance()
        instance2 = FunctionRegistry.get_instance()
        assert instance1 is instance2

    def test_global_registry(self) -> None:
        """Global registry should work."""
        # Clean up any existing registration
        try:
            registry.get("global_test")
            # If it exists, we need to use a unique name
            func_name = f"global_test_{id(self)}"
        except KeyError:
            func_name = "global_test"

        @registry.register(func_name)
        def global_func() -> str:
            return "global"

        assert registry.get(func_name)() == "global"

    def test_register_lambda(self) -> None:
        """Registry should handle lambdas."""
        test_registry = FunctionRegistry()

        # Lambdas don't have meaningful names, so provide one
        test_registry._functions["my_lambda"] = lambda x: x + 1

        retrieved = test_registry.get("my_lambda")
        assert retrieved(10) == 11


class TestGraphRebuild:
    """Test Option 3: Graph rebuild in activity.

    This is the recommended approach where the activity:
    1. Imports the graph builder function
    2. Calls it to get the compiled graph
    3. Gets the node by name from the graph
    """

    def test_graph_nodes_accessible(self) -> None:
        """Verify compiled graph nodes are accessible by name."""
        from langgraph.graph import END, START, StateGraph

        class SimpleState(TypedDict, total=False):
            value: int

        def my_node(state: SimpleState) -> SimpleState:
            return {"value": state.get("value", 0) + 1}

        graph = StateGraph(SimpleState)
        graph.add_node("my_node", my_node)
        graph.add_edge(START, "my_node")
        graph.add_edge("my_node", END)
        compiled = graph.compile()

        # Verify we can access the node
        assert "my_node" in compiled.nodes
        node = compiled.nodes["my_node"]
        assert node is not None

    def test_graph_node_execution(self) -> None:
        """Verify node can be invoked directly."""
        from langgraph.graph import END, START, StateGraph

        class SimpleState(TypedDict, total=False):
            value: int

        def increment(state: SimpleState) -> SimpleState:
            return {"value": state.get("value", 0) + 10}

        graph = StateGraph(SimpleState)
        graph.add_node("increment", increment)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)
        compiled = graph.compile()

        # Get the node and invoke it
        node = compiled.nodes["increment"]
        # Note: PregelNode wraps the function, we can invoke it
        result = node.invoke({"value": 5})
        assert result == {"value": 15}

    def test_multiple_nodes_accessible(self) -> None:
        """Verify all nodes in a graph are accessible."""
        from langgraph.graph import END, START, StateGraph

        class SimpleState(TypedDict, total=False):
            value: int

        def node_a(state: SimpleState) -> SimpleState:
            return {"value": 1}

        def node_b(state: SimpleState) -> SimpleState:
            return {"value": 2}

        graph = StateGraph(SimpleState)
        graph.add_node("node_a", node_a)
        graph.add_node("node_b", node_b)
        graph.add_edge(START, "node_a")
        graph.add_edge(START, "node_b")
        graph.add_edge("node_a", END)
        graph.add_edge("node_b", END)
        compiled = graph.compile()

        # Both nodes should be accessible
        assert "node_a" in compiled.nodes
        assert "node_b" in compiled.nodes

    def test_builder_function_pattern(self) -> None:
        """Test the builder function pattern used for activities."""
        from langgraph.graph import END, START, StateGraph

        class AgentState(TypedDict, total=False):
            messages: list[str]

        # This is the pattern: a builder function that returns compiled graph
        def build_my_graph() -> Any:
            def process(state: AgentState) -> AgentState:
                msgs = list(state.get("messages", []))
                msgs.append("processed")
                return {"messages": msgs}

            graph = StateGraph(AgentState)
            graph.add_node("process", process)
            graph.add_edge(START, "process")
            graph.add_edge("process", END)
            return graph.compile()

        # In an activity, we would:
        # 1. Import the builder (here we have it directly)
        compiled = build_my_graph()

        # 2. Get the node
        assert "process" in compiled.nodes
        node = compiled.nodes["process"]

        # 3. Invoke the node
        result = node.invoke({"messages": ["hello"]})
        assert "processed" in result["messages"]


class TestInspectGraph:
    """Test graph inspection utilities."""

    def test_inspect_returns_node_names(self) -> None:
        """Inspect should return node names."""
        from langgraph.graph import END, START, StateGraph

        class SimpleState(TypedDict, total=False):
            value: int

        def node_x(state: SimpleState) -> SimpleState:
            return {"value": 1}

        def node_y(state: SimpleState) -> SimpleState:
            return {"value": 2}

        graph = StateGraph(SimpleState)
        graph.add_node("node_x", node_x)
        graph.add_node("node_y", node_y)
        graph.add_edge(START, "node_x")
        graph.add_edge("node_x", "node_y")
        graph.add_edge("node_y", END)
        compiled = graph.compile()

        # Inspect the graph
        node_names = list(compiled.nodes.keys())
        assert "node_x" in node_names
        assert "node_y" in node_names

    def test_compiled_graph_has_no_checkpointer_by_default(self) -> None:
        """Compiled graph without checkpointer should report None."""
        from langgraph.graph import END, START, StateGraph

        class SimpleState(TypedDict, total=False):
            value: int

        def node(state: SimpleState) -> SimpleState:
            return {"value": 1}

        graph = StateGraph(SimpleState)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        compiled = graph.compile()

        assert compiled.checkpointer is None
