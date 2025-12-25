"""Tests for thread-safe graph registry mechanism.

These tests validate that:
1. Graph caching works correctly
2. Thread-safe concurrent access
3. Lambdas and closures are preserved
4. Node lookup and direct invocation work
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pytest
from typing_extensions import TypedDict

from temporalio.contrib.langgraph._prototypes.graph_registry_proto import (
    GraphRegistry,
    build_graph_with_class_methods,
    build_graph_with_lambda,
    build_graph_with_named_functions,
)


class TestGraphRegistry:
    """Test basic registry operations."""

    def test_register_and_get(self) -> None:
        """Registry should cache graph after first access."""
        registry = GraphRegistry()
        registry.register("test_graph", build_graph_with_named_functions)

        # First access builds and caches
        graph1 = registry.get_graph("test_graph")
        assert graph1 is not None
        assert registry.get_build_count("test_graph") == 1

        # Second access returns cached
        graph2 = registry.get_graph("test_graph")
        assert graph1 is graph2
        assert registry.get_build_count("test_graph") == 1

    def test_get_nonexistent_raises(self) -> None:
        """Getting nonexistent graph should raise KeyError."""
        registry = GraphRegistry()

        with pytest.raises(KeyError, match="not found"):
            registry.get_graph("nonexistent")

    def test_multiple_graphs(self) -> None:
        """Registry should handle multiple graphs independently."""
        registry = GraphRegistry()
        registry.register("graph_a", build_graph_with_lambda)
        registry.register("graph_b", build_graph_with_named_functions)

        graph_a = registry.get_graph("graph_a")
        graph_b = registry.get_graph("graph_b")

        assert graph_a is not graph_b
        assert registry.get_build_count("graph_a") == 1
        assert registry.get_build_count("graph_b") == 1


class TestLambdaPreservation:
    """Test that lambdas work correctly in cached graphs."""

    def test_lambda_with_closure(self) -> None:
        """Lambda with closure variables should work."""
        registry = GraphRegistry()
        registry.register("lambda_graph", build_graph_with_lambda)

        graph = registry.get_graph("lambda_graph")
        result = graph.invoke({"value": 3})

        # value: 3 * 10 (multiply) + 5 (offset) = 35
        assert result["value"] == 35
        assert "multiply_lambda" in result["processed_by"]
        assert "add_offset_lambda" in result["processed_by"]

    def test_lambda_multiple_invocations(self) -> None:
        """Cached lambda should work for multiple invocations."""
        registry = GraphRegistry()
        registry.register("lambda_graph", build_graph_with_lambda)

        graph = registry.get_graph("lambda_graph")

        # Multiple invocations with different inputs
        for input_val in [1, 5, 10, 100]:
            result = graph.invoke({"value": input_val})
            expected = input_val * 10 + 5
            assert result["value"] == expected


class TestClassMethodPreservation:
    """Test that class methods with instance state work."""

    def test_class_method_with_instance_state(self) -> None:
        """Class methods should preserve instance state."""
        registry = GraphRegistry()
        registry.register("class_graph", build_graph_with_class_methods)

        graph = registry.get_graph("class_graph")
        result = graph.invoke({"value": 2})

        # value: 2 * 3 (process_3x) * 7 (process_7x) = 42
        assert result["value"] == 42
        assert "processor_3" in result["processed_by"]
        assert "processor_7" in result["processed_by"]


class TestNodeLookup:
    """Test node lookup and direct invocation."""

    def test_get_node_by_name(self) -> None:
        """Should be able to get node by name."""
        registry = GraphRegistry()
        registry.register("named_graph", build_graph_with_named_functions)

        node = registry.get_node("named_graph", "increment")
        assert node is not None
        assert type(node).__name__ == "PregelNode"

    def test_get_nonexistent_node_raises(self) -> None:
        """Getting nonexistent node should raise KeyError."""
        registry = GraphRegistry()
        registry.register("named_graph", build_graph_with_named_functions)

        with pytest.raises(KeyError, match="not found"):
            registry.get_node("named_graph", "nonexistent_node")

    def test_node_direct_invocation(self) -> None:
        """Node should be directly invocable."""
        registry = GraphRegistry()
        registry.register("named_graph", build_graph_with_named_functions)

        node = registry.get_node("named_graph", "double")
        result = node.invoke({"value": 10})

        assert result["value"] == 20


class TestThreadSafety:
    """Test thread-safe concurrent access."""

    def test_concurrent_access_same_graph(self) -> None:
        """Multiple threads accessing same graph should work."""
        registry = GraphRegistry()
        registry.register("lambda_graph", build_graph_with_lambda)

        num_threads = 10
        iterations = 20
        errors: list[str] = []

        def worker(thread_id: int) -> list[bool]:
            results = []
            for i in range(iterations):
                try:
                    graph = registry.get_graph("lambda_graph")
                    input_val = thread_id * 100 + i
                    result = graph.invoke({"value": input_val})
                    expected = input_val * 10 + 5
                    results.append(result["value"] == expected)
                except Exception as e:
                    errors.append(str(e))
                    results.append(False)
            return results

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            all_results = []
            for future in as_completed(futures):
                all_results.extend(future.result())

        assert len(errors) == 0, f"Errors: {errors}"
        assert all(all_results)
        assert registry.get_build_count("lambda_graph") == 1

    def test_concurrent_node_invocation(self) -> None:
        """Multiple threads invoking nodes directly should work."""
        registry = GraphRegistry()
        registry.register("named_graph", build_graph_with_named_functions)

        num_threads = 10
        iterations = 20

        def worker(thread_id: int) -> list[bool]:
            results = []
            for i in range(iterations):
                node = registry.get_node("named_graph", "double")
                input_val = thread_id * 100 + i
                result = node.invoke({"value": input_val})
                results.append(result["value"] == input_val * 2)
            return results

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            all_results = []
            for future in as_completed(futures):
                all_results.extend(future.result())

        assert all(all_results)
        assert registry.get_build_count("named_graph") == 1

    def test_concurrent_different_graphs(self) -> None:
        """Multiple threads accessing different graphs should work."""
        registry = GraphRegistry()
        registry.register("lambda_graph", build_graph_with_lambda)
        registry.register("named_graph", build_graph_with_named_functions)
        registry.register("class_graph", build_graph_with_class_methods)

        num_threads = 12

        def worker(thread_id: int) -> bool:
            graph_ids = ["lambda_graph", "named_graph", "class_graph"]
            graph_id = graph_ids[thread_id % 3]
            graph = registry.get_graph(graph_id)
            result = graph.invoke({"value": 2})
            return result["value"] is not None

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            results = [future.result() for future in as_completed(futures)]

        assert all(results)
        # Each graph should be built exactly once
        assert registry.get_build_count("lambda_graph") == 1
        assert registry.get_build_count("named_graph") == 1
        assert registry.get_build_count("class_graph") == 1
