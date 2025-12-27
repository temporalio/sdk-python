"""LangGraph integration exceptions."""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

# Error type constants for ApplicationError.type
GRAPH_NOT_FOUND_ERROR = "LangGraphNotFound"
NODE_NOT_FOUND_ERROR = "LangGraphNodeNotFound"
TOOL_NOT_FOUND_ERROR = "LangGraphToolNotFound"
MODEL_NOT_FOUND_ERROR = "LangGraphModelNotFound"
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


def tool_not_found_error(tool_name: str, available: list[str]) -> ApplicationError:
    """Create an ApplicationError for a missing tool."""
    return ApplicationError(
        f"Tool '{tool_name}' not found in registry. "
        f"Available tools: {available}. "
        "Ensure the tool is wrapped with temporal_tool() and registered.",
        tool_name,
        available,
        type=TOOL_NOT_FOUND_ERROR,
        non_retryable=True,
    )


def model_not_found_error(model_name: str, available: list[str]) -> ApplicationError:
    """Create an ApplicationError for a missing model."""
    return ApplicationError(
        f"Model '{model_name}' not found in registry. "
        f"Available models: {available}. "
        "Ensure the model is wrapped with temporal_model() and registered.",
        model_name,
        available,
        type=MODEL_NOT_FOUND_ERROR,
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


class ToolAlreadyRegisteredError(ValueError):
    """Raised when registering a tool with a duplicate name."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' is already registered.")


class ModelAlreadyRegisteredError(ValueError):
    """Raised when registering a model with a duplicate name."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(f"Model '{model_name}' is already registered.")
