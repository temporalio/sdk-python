"""Prototype: Thread-safe Graph Registry and Node Lookup.

Technical Concern:
    Can we cache compiled graphs per worker process and look up nodes
    from multiple threads safely? Do lambdas work correctly?

Tests:
    1. Graph registry with caching
    2. Thread-safe concurrent access
    3. Lambda preservation in cached graphs
    4. Node lookup and execution

VALIDATION STATUS: [PENDING]
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.pregel import Pregel
from typing_extensions import TypedDict


# ============================================================================
# Graph Registry Implementation (matches V3.1 proposal)
# ============================================================================

class GraphRegistry:
    """Thread-safe registry for graph builders and cached compiled graphs.

    This is the core of the V3.1 plugin architecture:
    - Builders are registered by ID
    - Compiled graphs are cached on first access
    - Cache is thread-safe via locking
    """

    def __init__(self) -> None:
        self._builders: dict[str, Callable[[], Pregel]] = {}
        self._cache: dict[str, Pregel] = {}
        self._lock = threading.Lock()
        self._build_count: dict[str, int] = {}  # Track how many times each graph is built

    def register(self, graph_id: str, builder: Callable[[], Pregel]) -> None:
        """Register a graph builder by ID."""
        with self._lock:
            self._builders[graph_id] = builder
            self._build_count[graph_id] = 0

    def get_graph(self, graph_id: str) -> Pregel:
        """Get compiled graph by ID, building and caching if needed.

        Thread-safe: uses locking for cache access.
        """
        # Fast path: check cache without lock first (read is atomic for dict)
        if graph_id in self._cache:
            return self._cache[graph_id]

        # Slow path: acquire lock and build if needed
        with self._lock:
            # Double-check after acquiring lock
            if graph_id in self._cache:
                return self._cache[graph_id]

            if graph_id not in self._builders:
                raise KeyError(
                    f"Graph '{graph_id}' not found. "
                    f"Available: {list(self._builders.keys())}"
                )

            # Build and cache
            builder = self._builders[graph_id]
            graph = builder()
            self._cache[graph_id] = graph
            self._build_count[graph_id] += 1
            return graph

    def get_node(self, graph_id: str, node_name: str) -> Any:
        """Get a specific node's runnable from a cached graph."""
        graph = self.get_graph(graph_id)

        if node_name not in graph.nodes:
            raise KeyError(
                f"Node '{node_name}' not found in graph '{graph_id}'. "
                f"Available: {list(graph.nodes.keys())}"
            )

        return graph.nodes[node_name]

    def get_build_count(self, graph_id: str) -> int:
        """Get how many times a graph was built (should be 1 after caching)."""
        with self._lock:
            return self._build_count.get(graph_id, 0)

    def clear_cache(self) -> None:
        """Clear the cache (for testing)."""
        with self._lock:
            self._cache.clear()
            for key in self._build_count:
                self._build_count[key] = 0


# Global registry instance (simulates what plugin would create)
_registry = GraphRegistry()


# ============================================================================
# Test Graphs with Various Node Types
# ============================================================================

class SimpleState(TypedDict, total=False):
    value: int
    processed_by: list[str]


def build_graph_with_lambda() -> Pregel:
    """Build a graph that uses lambda functions."""

    # Closure variable to verify lambdas capture correctly
    multiplier = 10

    graph = StateGraph(SimpleState)

    # Lambda node
    graph.add_node("multiply", lambda state: {
        "value": state.get("value", 0) * multiplier,
        "processed_by": state.get("processed_by", []) + ["multiply_lambda"],
    })

    # Another lambda with different closure
    offset = 5
    graph.add_node("add_offset", lambda state: {
        "value": state.get("value", 0) + offset,
        "processed_by": state.get("processed_by", []) + ["add_offset_lambda"],
    })

    graph.add_edge(START, "multiply")
    graph.add_edge("multiply", "add_offset")
    graph.add_edge("add_offset", END)

    return graph.compile()


def build_graph_with_named_functions() -> Pregel:
    """Build a graph with named functions."""

    def increment(state: SimpleState) -> SimpleState:
        return {
            "value": state.get("value", 0) + 1,
            "processed_by": state.get("processed_by", []) + ["increment"],
        }

    def double(state: SimpleState) -> SimpleState:
        return {
            "value": state.get("value", 0) * 2,
            "processed_by": state.get("processed_by", []) + ["double"],
        }

    graph = StateGraph(SimpleState)
    graph.add_node("increment", increment)
    graph.add_node("double", double)
    graph.add_edge(START, "increment")
    graph.add_edge("increment", "double")
    graph.add_edge("double", END)

    return graph.compile()


def build_graph_with_class_methods() -> Pregel:
    """Build a graph using class methods (common pattern)."""

    class Processor:
        def __init__(self, factor: int):
            self.factor = factor

        def process(self, state: SimpleState) -> SimpleState:
            return {
                "value": state.get("value", 0) * self.factor,
                "processed_by": state.get("processed_by", []) + [f"processor_{self.factor}"],
            }

    p1 = Processor(3)
    p2 = Processor(7)

    graph = StateGraph(SimpleState)
    graph.add_node("process_3x", p1.process)
    graph.add_node("process_7x", p2.process)
    graph.add_edge(START, "process_3x")
    graph.add_edge("process_3x", "process_7x")
    graph.add_edge("process_7x", END)

    return graph.compile()


# ============================================================================
# Test Functions
# ============================================================================

def test_basic_registry() -> dict[str, Any]:
    """Test basic registry operations."""
    registry = GraphRegistry()

    # Register graphs
    registry.register("lambda_graph", build_graph_with_lambda)
    registry.register("named_graph", build_graph_with_named_functions)

    # Get graph (should build and cache)
    graph1 = registry.get_graph("lambda_graph")
    assert graph1 is not None
    assert registry.get_build_count("lambda_graph") == 1

    # Get again (should return cached)
    graph2 = registry.get_graph("lambda_graph")
    assert graph1 is graph2  # Same instance
    assert registry.get_build_count("lambda_graph") == 1  # Not rebuilt

    # Get different graph
    graph3 = registry.get_graph("named_graph")
    assert graph3 is not None
    assert graph3 is not graph1
    assert registry.get_build_count("named_graph") == 1

    return {
        "success": True,
        "lambda_graph_cached": graph1 is graph2,
        "build_counts": {
            "lambda_graph": registry.get_build_count("lambda_graph"),
            "named_graph": registry.get_build_count("named_graph"),
        }
    }


def test_lambda_preservation() -> dict[str, Any]:
    """Test that lambdas work correctly in cached graphs."""
    registry = GraphRegistry()
    registry.register("lambda_graph", build_graph_with_lambda)

    # Get graph and execute
    graph = registry.get_graph("lambda_graph")

    # Execute the graph
    result = graph.invoke({"value": 3})

    # value: 3 * 10 (multiply) + 5 (offset) = 35
    expected_value = 35

    return {
        "success": result["value"] == expected_value,
        "input": 3,
        "expected": expected_value,
        "actual": result["value"],
        "processed_by": result.get("processed_by", []),
    }


def test_node_lookup() -> dict[str, Any]:
    """Test looking up specific nodes."""
    registry = GraphRegistry()
    registry.register("named_graph", build_graph_with_named_functions)

    # Get specific node
    node = registry.get_node("named_graph", "increment")

    # Node should be a PregelNode
    assert node is not None

    # Can we invoke it directly?
    result = node.invoke({"value": 10})

    return {
        "success": result["value"] == 11,
        "node_type": type(node).__name__,
        "input": 10,
        "expected": 11,
        "actual": result["value"],
    }


def test_concurrent_access() -> dict[str, Any]:
    """Test thread-safe concurrent access to registry."""
    registry = GraphRegistry()
    registry.register("lambda_graph", build_graph_with_lambda)

    num_threads = 20
    iterations_per_thread = 50
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    def worker(thread_id: int) -> dict[str, Any]:
        """Worker function that accesses the registry."""
        thread_results = []
        for i in range(iterations_per_thread):
            try:
                # Get graph (should always return same cached instance)
                graph = registry.get_graph("lambda_graph")

                # Execute with unique input
                input_value = thread_id * 1000 + i
                result = graph.invoke({"value": input_value})

                expected = input_value * 10 + 5
                thread_results.append({
                    "thread_id": thread_id,
                    "iteration": i,
                    "input": input_value,
                    "output": result["value"],
                    "expected": expected,
                    "correct": result["value"] == expected,
                })
            except Exception as e:
                errors.append(f"Thread {thread_id}, iteration {i}: {e}")

        return {
            "thread_id": thread_id,
            "completed": len(thread_results),
            "all_correct": all(r["correct"] for r in thread_results),
        }

    # Run concurrent threads
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.time() - start_time

    # Verify graph was only built once despite concurrent access
    build_count = registry.get_build_count("lambda_graph")

    return {
        "success": len(errors) == 0 and build_count == 1,
        "num_threads": num_threads,
        "iterations_per_thread": iterations_per_thread,
        "total_operations": num_threads * iterations_per_thread,
        "elapsed_seconds": round(elapsed, 3),
        "build_count": build_count,
        "all_correct": all(r["all_correct"] for r in results),
        "errors": errors[:5] if errors else [],  # First 5 errors
    }


def test_concurrent_different_graphs() -> dict[str, Any]:
    """Test concurrent access to different graphs."""
    registry = GraphRegistry()
    registry.register("lambda_graph", build_graph_with_lambda)
    registry.register("named_graph", build_graph_with_named_functions)
    registry.register("class_graph", build_graph_with_class_methods)

    num_threads = 15
    errors: list[str] = []

    def worker(thread_id: int) -> dict[str, Any]:
        """Worker that accesses different graphs based on thread_id."""
        graph_ids = ["lambda_graph", "named_graph", "class_graph"]
        graph_id = graph_ids[thread_id % 3]

        try:
            graph = registry.get_graph(graph_id)
            result = graph.invoke({"value": 2})
            return {
                "thread_id": thread_id,
                "graph_id": graph_id,
                "result": result["value"],
                "success": True,
            }
        except Exception as e:
            return {
                "thread_id": thread_id,
                "graph_id": graph_id,
                "error": str(e),
                "success": False,
            }

    # Run concurrent threads
    results = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for future in as_completed(futures):
            results.append(future.result())

    return {
        "success": all(r["success"] for r in results),
        "build_counts": {
            "lambda_graph": registry.get_build_count("lambda_graph"),
            "named_graph": registry.get_build_count("named_graph"),
            "class_graph": registry.get_build_count("class_graph"),
        },
        "all_built_once": all(
            registry.get_build_count(gid) == 1
            for gid in ["lambda_graph", "named_graph", "class_graph"]
        ),
        "thread_results": results[:5],  # First 5 results
    }


def test_class_method_preservation() -> dict[str, Any]:
    """Test that class methods with instance state work."""
    registry = GraphRegistry()
    registry.register("class_graph", build_graph_with_class_methods)

    graph = registry.get_graph("class_graph")
    result = graph.invoke({"value": 2})

    # value: 2 * 3 (process_3x) * 7 (process_7x) = 42
    expected = 42

    return {
        "success": result["value"] == expected,
        "input": 2,
        "expected": expected,
        "actual": result["value"],
        "processed_by": result.get("processed_by", []),
    }


def test_node_direct_invocation_concurrent() -> dict[str, Any]:
    """Test concurrent direct node invocation (simulates activity calls)."""
    registry = GraphRegistry()
    registry.register("named_graph", build_graph_with_named_functions)

    num_threads = 10
    iterations = 20

    def worker(thread_id: int) -> list[dict]:
        """Simulate activity: get node and invoke it."""
        results = []
        for i in range(iterations):
            # This is what an activity would do:
            node = registry.get_node("named_graph", "double")
            input_value = thread_id * 100 + i
            result = node.invoke({"value": input_value})

            results.append({
                "input": input_value,
                "output": result["value"],
                "expected": input_value * 2,
                "correct": result["value"] == input_value * 2,
            })
        return results

    all_results = []
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        for future in as_completed(futures):
            all_results.extend(future.result())

    all_correct = all(r["correct"] for r in all_results)

    return {
        "success": all_correct and registry.get_build_count("named_graph") == 1,
        "total_invocations": len(all_results),
        "all_correct": all_correct,
        "build_count": registry.get_build_count("named_graph"),
    }


# ============================================================================
# Run All Tests
# ============================================================================

def run_all_tests() -> None:
    """Run all validation tests."""
    tests = [
        ("Basic Registry", test_basic_registry),
        ("Lambda Preservation", test_lambda_preservation),
        ("Node Lookup", test_node_lookup),
        ("Class Method Preservation", test_class_method_preservation),
        ("Concurrent Access (Same Graph)", test_concurrent_access),
        ("Concurrent Access (Different Graphs)", test_concurrent_different_graphs),
        ("Concurrent Node Invocation", test_node_direct_invocation_concurrent),
    ]

    print("=" * 70)
    print("Graph Registry Thread-Safety Validation")
    print("=" * 70)

    all_passed = True
    for name, test_func in tests:
        print(f"\n>>> {name}")
        try:
            result = test_func()
            passed = result.get("success", False)
            all_passed = all_passed and passed

            status = "✅ PASSED" if passed else "❌ FAILED"
            print(f"    Status: {status}")

            # Print relevant details
            for key, value in result.items():
                if key != "success":
                    print(f"    {key}: {value}")

        except Exception as e:
            all_passed = False
            print(f"    Status: ❌ ERROR")
            print(f"    Exception: {e}")

    print("\n" + "=" * 70)
    print(f"OVERALL: {'✅ ALL TESTS PASSED' if all_passed else '❌ SOME TESTS FAILED'}")
    print("=" * 70)


if __name__ == "__main__":
    run_all_tests()
