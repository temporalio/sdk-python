"""Prototype 5: Graph Reconstruction in Activities.

Technical Concern:
    How do activities get access to node functions to execute them?
    The graph is built in the workflow, but activities run in a separate
    worker process.

Options Explored:
    1. Import function by module path string
    2. Use a function registry
    3. Rebuild graph in activity and get node by name

FINDINGS:
    Option 1 (Module Import): Works, but requires functions to be importable
        - Functions must be defined in importable modules (not __main__)
        - Activity receives module path as string, imports at runtime
        - Simple and follows standard Python patterns

    Option 2 (Registry): Works, more flexible
        - Functions registered by name
        - Activity looks up by name in registry
        - Supports lambdas and closures (but beware serialization)

    Option 3 (Rebuild Graph): Recommended approach
        - Activity receives graph builder module path
        - Activity calls builder to get compiled graph
        - Activity gets node by name from graph
        - Most consistent with proposal architecture

Recommended: Option 3 (Rebuild Graph)
    - Graph is defined in a builder function
    - Builder function is importable by module path
    - Activity imports builder, builds graph, gets node
    - Same graph structure in workflow and activity

VALIDATION STATUS: PASSED
    - Module import works for importable functions
    - Registry pattern works for flexible lookup
    - Graph rebuild is most robust approach
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, TypeVar

T = TypeVar("T")


# --- Option 1: Import by module path ---


def import_function(module_path: str) -> Callable[..., Any]:
    """Import a function by its module path.

    Args:
        module_path: Full module path like "myapp.agents.fetch_data"

    Returns:
        The imported function

    Raises:
        ImportError: If module or function not found
    """
    parts = module_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ImportError(f"Invalid module path: {module_path}")

    module_name, func_name = parts
    module = importlib.import_module(module_name)
    func = getattr(module, func_name, None)

    if func is None:
        raise ImportError(f"Function {func_name} not found in {module_name}")

    return func


# --- Option 2: Function Registry ---


class FunctionRegistry:
    """Registry for looking up functions by name.

    This allows activities to find node functions without module paths.
    """

    _instance: FunctionRegistry | None = None
    _functions: dict[str, Callable[..., Any]]

    def __init__(self) -> None:
        self._functions = {}

    @classmethod
    def get_instance(cls) -> FunctionRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(
        self, name: str | None = None
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator to register a function.

        Args:
            name: Optional name. If not provided, uses function's __name__.

        Returns:
            Decorator function.
        """

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            key = name or func.__name__
            self._functions[key] = func
            return func

        return decorator

    def get(self, name: str) -> Callable[..., Any]:
        """Get a function by name.

        Args:
            name: Function name

        Returns:
            The registered function

        Raises:
            KeyError: If function not found
        """
        if name not in self._functions:
            raise KeyError(f"Function '{name}' not found in registry")
        return self._functions[name]

    def clear(self) -> None:
        """Clear all registered functions."""
        self._functions.clear()


# Global registry instance
registry = FunctionRegistry.get_instance()


# --- Option 3: Graph Rebuild ---


def get_node_from_graph_builder(
    builder_path: str,
    node_name: str,
) -> Any:
    """Get a node function by rebuilding the graph.

    This is the recommended approach:
    1. Import the graph builder function
    2. Call it to get the compiled graph
    3. Get the node by name from the graph

    Args:
        builder_path: Module path to graph builder function
        node_name: Name of the node to get

    Returns:
        The node's runnable/function

    Example:
        # In myapp/agents.py:
        def build_agent_graph():
            graph = StateGraph(AgentState)
            graph.add_node("fetch", fetch_data)
            # ...
            return graph.compile()

        # In activity:
        node = get_node_from_graph_builder(
            "myapp.agents.build_agent_graph",
            "fetch"
        )
    """
    from langgraph.pregel import Pregel

    # Import the builder function
    builder_func = import_function(builder_path)

    # Build the graph
    compiled_graph: Pregel = builder_func()

    # Get the node
    if node_name not in compiled_graph.nodes:
        available = list(compiled_graph.nodes.keys())
        raise KeyError(f"Node '{node_name}' not found. Available: {available}")

    return compiled_graph.nodes[node_name]


def inspect_compiled_graph(builder_path: str) -> dict[str, Any]:
    """Inspect a compiled graph's structure.

    Useful for debugging and understanding graph structure.

    Args:
        builder_path: Module path to graph builder function

    Returns:
        Dict with graph structure information
    """
    from langgraph.pregel import Pregel

    builder_func = import_function(builder_path)
    compiled_graph: Pregel = builder_func()

    return {
        "node_names": list(compiled_graph.nodes.keys()),
        "node_count": len(compiled_graph.nodes),
        "has_checkpointer": compiled_graph.checkpointer is not None,
        "stream_mode": compiled_graph.stream_mode,
    }


# --- Example usage and testing ---


if __name__ == "__main__":
    from typing_extensions import TypedDict

    from langgraph.graph import END, START, StateGraph

    # Define a simple state
    class DemoState(TypedDict, total=False):
        value: int

    # Option 1: Test module import
    print("=== Option 1: Module Import ===")
    try:
        # This would work for any importable function
        func = import_function("json.dumps")
        print(f"Imported: {func}")
        print(f"Result: {func({'test': 'value'})}")
    except ImportError as e:
        print(f"Import failed: {e}")

    # Option 2: Test registry
    print("\n=== Option 2: Function Registry ===")

    @registry.register("my_node")
    def demo_node(state: DemoState) -> DemoState:
        return {"value": state.get("value", 0) + 1}

    found = registry.get("my_node")
    print(f"Found function: {found}")
    print(f"Result: {found({'value': 10})}")

    # Option 3 would require a separate module file
    # but the pattern is demonstrated in get_node_from_graph_builder
    print("\n=== Option 3: Graph Rebuild ===")
    print("(Requires external module - see tests for full example)")
