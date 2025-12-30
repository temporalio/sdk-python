"""LangGraph integration exceptions."""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

# Error type constants for ApplicationError.type
GRAPH_NOT_FOUND_ERROR = "LangGraphNotFound"
NODE_NOT_FOUND_ERROR = "LangGraphNodeNotFound"
GRAPH_DEFINITION_CHANGED_ERROR = "LangGraphDefinitionChanged"
NODE_EXECUTION_ERROR = "LangGraphNodeExecutionError"


def is_non_retryable_error(exc: BaseException) -> bool:
    """Determine if an exception should be marked as non-retryable.

    Non-retryable errors are those that will fail again if retried:
    - Configuration/authentication errors (invalid API key, model not found)
    - Validation errors (bad input, invalid parameters)
    - Programming bugs (type errors, attribute errors)

    Retryable errors (returns False) include:
    - Rate limit errors (429)
    - Network/connection errors
    - Temporary server errors (500, 502, 503, 504)
    - Timeout errors

    Returns:
        True if the error should NOT be retried, False if it should be retried.
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__

    # Python built-in errors that indicate bugs or bad input - never retry
    non_retryable_types = {
        "TypeError",
        "ValueError",
        "KeyError",
        "AttributeError",
        "IndexError",
        "AssertionError",
        "NotImplementedError",
        "SyntaxError",
        "NameError",
        "ImportError",
        "ModuleNotFoundError",
    }
    if exc_type in non_retryable_types:
        return True

    # OpenAI SDK errors (openai module)
    if "openai" in exc_module:
        # Non-retryable OpenAI errors
        if exc_type in {
            "AuthenticationError",  # Invalid API key
            "PermissionDeniedError",  # No access to resource
            "BadRequestError",  # Malformed request
            "NotFoundError",  # Model/resource not found
            "UnprocessableEntityError",  # Invalid parameters
            "ContentFilterFinishReasonError",  # Content policy violation
        }:
            return True
        # Retryable OpenAI errors - let them pass through
        # RateLimitError, APIConnectionError, InternalServerError, APITimeoutError
        return False

    # Anthropic SDK errors
    if "anthropic" in exc_module:
        if exc_type in {
            "AuthenticationError",
            "PermissionDeniedError",
            "BadRequestError",
            "NotFoundError",
        }:
            return True
        return False

    # LangChain errors
    if "langchain" in exc_module:
        if exc_type in {
            "OutputParserException",  # Bad LLM output format
        }:
            return True
        return False

    # HTTP status-based classification (for generic HTTP errors)
    # Check for status_code attribute
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        # 4xx client errors (except 429) are non-retryable
        if 400 <= status_code < 500 and status_code != 429:
            return True
        # 429 (rate limit) and 5xx are retryable
        return False

    # Default: assume retryable to be safe
    # Better to retry unnecessarily than to fail permanently on a transient error
    return False


def node_execution_error(
    node_name: str,
    graph_id: str,
    original_error: BaseException,
    non_retryable: bool,
) -> ApplicationError:
    """Create an ApplicationError for node execution failure.

    Args:
        node_name: Name of the node that failed.
        graph_id: ID of the graph containing the node.
        original_error: The original exception that caused the failure.
        non_retryable: Whether this error should NOT be retried.

    Returns:
        An ApplicationError with appropriate retry semantics.
    """
    error_type = type(original_error).__name__
    return ApplicationError(
        f"Node '{node_name}' in graph '{graph_id}' failed: {error_type}: {original_error}",
        node_name,
        graph_id,
        error_type,
        str(original_error),
        type=NODE_EXECUTION_ERROR,
        non_retryable=non_retryable,
    )


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
        """Initialize with the duplicate graph ID."""
        self.graph_id = graph_id
        super().__init__(f"Graph '{graph_id}' is already registered.")
