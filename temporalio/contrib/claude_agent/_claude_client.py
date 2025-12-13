"""Claude client wrapper for use in workflows - DEPRECATED."""

from typing import Any, AsyncIterator


class WorkflowClaudeClient:
    """DEPRECATED - Use SimplifiedClaudeClient instead.

    This class is deprecated and will be removed in a future version.
    Please use SimplifiedClaudeClient from temporalio.contrib.claude_agent.
    """

    def __init__(self, session_name: str):
        """Initialize the client.

        Args:
            session_name: Name of the session to connect to
        """
        raise NotImplementedError(
            "WorkflowClaudeClient is deprecated. Use SimplifiedClaudeClient instead."
        )

    async def connect(self) -> None:
        """Connect to the Claude session."""
        raise NotImplementedError(
            "WorkflowClaudeClient is deprecated. Use SimplifiedClaudeClient instead."
        )

    async def send_query(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        """Send a query to Claude and receive responses.

        Args:
            prompt: The prompt to send to Claude

        Yields:
            Response messages from Claude
        """
        raise NotImplementedError(
            "WorkflowClaudeClient is deprecated. Use SimplifiedClaudeClient instead."
        )
        yield {}  # Make this an async generator for type checking

    async def close(self) -> None:
        """Close the client."""
        raise NotImplementedError(
            "WorkflowClaudeClient is deprecated. Use SimplifiedClaudeClient instead."
        )