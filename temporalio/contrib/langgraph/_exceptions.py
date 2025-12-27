"""LangGraph integration exceptions."""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

# Error type constants for ApplicationError.type
GRAPH_NOT_FOUND_ERROR = "LangGraphNotFound"
NODE_NOT_FOUND_ERROR = "LangGraphNodeNotFound"
GRAPH_DEFINITION_CHANGED_ERROR = "LangGraphDefinitionChanged"


def graph_not_found_error(graph_id: str, available: list[str]) -> ApplicationError:
    """Create an ApplicationError for a missing graph."""
    return ApplicationError(
        f"Graph '{graph_id}' not found in registry. "
        f"Available graphs: {available}. "
        "Ensure the graph is registered with LangGraphPlugin.",
        graph_id,
        available,
        type=GRAPH_NOT_FOUND_ERROR,
        non_retryable=True,
    )


def node_not_found_error(
    node_name: str, graph_id: str, available: list[str]
) -> ApplicationError:
    """Create an ApplicationError for a missing node."""
    return ApplicationError(
        f"Node '{node_name}' not found in graph '{graph_id}'. "
        f"Available nodes: {available}",
        node_name,
        graph_id,
        available,
        type=NODE_NOT_FOUND_ERROR,
        non_retryable=True,
    )


def graph_definition_changed_error(
    graph_id: str, expected_nodes: list[str], actual_nodes: list[str]
) -> ApplicationError:
    """Create an ApplicationError for graph definition change during execution."""
    return ApplicationError(
        f"Graph '{graph_id}' definition changed during workflow execution. "
        f"Expected nodes: {expected_nodes}, actual nodes: {actual_nodes}. "
        "This can happen if the graph was modified between workflow runs. "
        "Consider versioning your workflows or using a new workflow ID.",
        graph_id,
        expected_nodes,
        actual_nodes,
        type=GRAPH_DEFINITION_CHANGED_ERROR,
        non_retryable=True,
    )


# Configuration exceptions (raised at setup, not during execution)


class GraphAlreadyRegisteredError(ValueError):
    """Raised when registering a graph with a duplicate ID."""

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        super().__init__(f"Graph '{graph_id}' is already registered.")


