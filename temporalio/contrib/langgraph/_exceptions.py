"""LangGraph integration exceptions.

This module provides domain-specific exceptions for the LangGraph integration.
Exceptions that cross workflow/activity boundaries use ApplicationError with
specific types, while configuration errors use standard Python exceptions.
"""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

# =============================================================================
# Error Type Constants
# =============================================================================
# These constants define the error types used with ApplicationError.
# They allow callers to catch specific error types when needed.

GRAPH_NOT_FOUND_ERROR = "LangGraphNotFound"
"""Error type for when a graph is not found in the registry."""

NODE_NOT_FOUND_ERROR = "LangGraphNodeNotFound"
"""Error type for when a node is not found in a graph."""

TOOL_NOT_FOUND_ERROR = "LangGraphToolNotFound"
"""Error type for when a tool is not found in the registry."""

MODEL_NOT_FOUND_ERROR = "LangGraphModelNotFound"
"""Error type for when a model is not found in the registry."""

GRAPH_DEFINITION_CHANGED_ERROR = "LangGraphDefinitionChanged"
"""Error type for when graph definition changes during execution."""


# =============================================================================
# Activity-Level Exceptions (Cross Workflow/Activity Boundary)
# =============================================================================
# These functions create ApplicationError instances with specific types.
# Use these for errors that occur in activities and need to propagate to workflows.


def graph_not_found_error(graph_id: str, available: list[str]) -> ApplicationError:
    """Create an error for when a graph is not found in the registry.

    Args:
        graph_id: The ID of the graph that was not found.
        available: List of available graph IDs.

    Returns:
        ApplicationError with type GRAPH_NOT_FOUND_ERROR and details.
    """
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
    """Create an error for when a node is not found in a graph.

    Args:
        node_name: The name of the node that was not found.
        graph_id: The ID of the graph being searched.
        available: List of available node names.

    Returns:
        ApplicationError with type NODE_NOT_FOUND_ERROR and details.
    """
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
    """Create an error for when a tool is not found in the registry.

    Args:
        tool_name: The name of the tool that was not found.
        available: List of available tool names.

    Returns:
        ApplicationError with type TOOL_NOT_FOUND_ERROR and details.
    """
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
    """Create an error for when a model is not found in the registry.

    Args:
        model_name: The name of the model that was not found.
        available: List of available model names.

    Returns:
        ApplicationError with type MODEL_NOT_FOUND_ERROR and details.
    """
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
    """Create an error for when graph definition changes during execution.

    This is a non-retryable error because it indicates a deployment issue
    where the graph was modified while a workflow was running.

    Args:
        graph_id: The ID of the graph.
        expected_nodes: The nodes expected based on workflow history.
        actual_nodes: The actual nodes in the current graph definition.

    Returns:
        ApplicationError with type GRAPH_DEFINITION_CHANGED_ERROR and details.
    """
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


# =============================================================================
# Configuration Exceptions (Do Not Cross Boundaries)
# =============================================================================
# These are raised during setup/configuration and don't need ApplicationError.


class GraphAlreadyRegisteredError(ValueError):
    """Raised when attempting to register a graph with an ID that already exists.

    This is a configuration error that occurs at worker startup, not during
    workflow/activity execution.
    """

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        super().__init__(
            f"Graph '{graph_id}' is already registered. "
            "Use a unique graph_id for each graph."
        )


class ToolAlreadyRegisteredError(ValueError):
    """Raised when attempting to register a tool with a name that already exists.

    This is a configuration error that occurs at worker startup, not during
    workflow/activity execution.
    """

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(
            f"Tool '{tool_name}' is already registered. "
            "Use a unique name for each tool."
        )


class ModelAlreadyRegisteredError(ValueError):
    """Raised when attempting to register a model with a name that already exists.

    This is a configuration error that occurs at worker startup, not during
    workflow/activity execution.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(
            f"Model '{model_name}' is already registered. "
            "Use a unique name for each model."
        )
