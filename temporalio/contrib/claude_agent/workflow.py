"""Workflow-specific primitives for working with Claude Agent SDK in a workflow context."""

from contextlib import AbstractAsyncContextManager
from typing import Any

from temporalio.workflow import ActivityConfig

from ._claude_client import WorkflowClaudeClient
from ._session_config import ClaudeSessionConfig

__all__ = ["claude_session", "create_claude_transport", "WorkflowClaudeClient"]


def claude_session(
    name: str,
    config: ClaudeSessionConfig,
    activity_config: ActivityConfig | None = None,
    original_claude_config: dict[str, Any] | None = None,
) -> AbstractAsyncContextManager[Any]:
    """Create a stateful Claude Agent session for use in Temporal workflows.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    This function creates a stateful Claude Code session that maintains context
    throughout the workflow execution. The session runs in a dedicated activity
    worker and preserves:
    - Conversation history
    - File system changes
    - Tool execution context

    The session must be used as a context manager and will be cleaned up when
    the context exits.

    Args:
        name: A string name for the session. Should match that provided in the plugin.
        config: Claude session configuration (ClaudeSessionConfig instance).
               Contains serializable parameters like system_prompt, max_turns, model, etc.
        activity_config: Optional activity configuration for I/O operations.
               Defaults to 1-minute start-to-close and 30-second schedule-to-start timeouts.
        session_config: Optional activity configuration for the session activity.
                       Defaults to 1-hour start-to-close timeout.

    Returns:
        An async context manager that manages the Claude session lifecycle.

    Example:
        >>> from temporalio import workflow
        >>> from temporalio.contrib.claude_agent import workflow as claude_workflow
        >>> from temporalio.contrib.claude_agent import ClaudeSessionConfig
        >>>
        >>> @workflow.defn
        >>> class ClaudeWorkflow:
        ...     @workflow.run
        ...     async def run(self, prompt: str) -> str:
        ...         config = ClaudeSessionConfig(
        ...             system_prompt="You are a helpful assistant",
        ...             max_turns=5,
        ...         )
        ...         async with claude_workflow.claude_session("my-session", config) as session:
        ...             # Use the session to interact with Claude
        ...             transport = claude_workflow.create_claude_transport("my-session")
        ...             # ... interact with Claude via transport
        ...         return "Done"

    Note:
        The caller must handle cases where the dedicated worker fails. If the
        worker fails, Temporal cannot seamlessly recreate the lost state.
    """
    from temporalio.contrib.claude_agent._stateful_session_v3 import (
        _StatefulClaudeSessionReference,
    )

    return _StatefulClaudeSessionReference(name, activity_config, config, original_claude_config)


def create_claude_transport(
    session_name: str,
    config: ActivityConfig | None = None,
) -> Any:
    """Create a transport for use with Claude Agent SDK in workflows.

    .. warning::
        This function is deprecated. Use SimplifiedClaudeClient instead.

    Args:
        session_name: Name of the session (must match the name used in claude_session)
        config: Optional activity configuration for I/O operations

    Returns:
        None - this function is deprecated

    """
    raise NotImplementedError(
        "create_claude_transport is deprecated. Use SimplifiedClaudeClient instead."
    )
