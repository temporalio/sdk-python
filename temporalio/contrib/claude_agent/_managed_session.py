"""Managed Claude session with pause/resume capability for resource optimization."""

from typing import Any

from ._session_config import ClaudeSessionConfig
from ._simple_client import SimplifiedClaudeClient
from . import workflow as claude_workflow


class ManagedClaudeSession:
    """Context manager for Claude sessions with pause/resume capability.

    This class provides explicit control over when the Claude activity is running,
    allowing workflows to free resources during idle periods. Claude's session state
    (conversation history, filesystem changes, etc.) persists across pause/resume
    cycles because Claude stores its state on the filesystem.

    Example:
        >>> @workflow.defn
        >>> class MyWorkflow(ClaudeMessageReceiver):
        ...     @workflow.run
        ...     async def run(self):
        ...         self.init_claude_receiver()
        ...         config = ClaudeSessionConfig(system_prompt="You are helpful")
        ...
        ...         async with ManagedClaudeSession("my-session", config, self) as session:
        ...             # Session is connected, activity running
        ...             result1 = await session.send_query("Create file test.py")
        ...
        ...             # Pause to free resources
        ...             await session.pause()
        ...
        ...             # Sleep without consuming resources
        ...             await asyncio.sleep(3600)
        ...
        ...             # Resume and continue - Claude remembers test.py
        ...             result2 = await session.send_query("What's in test.py?")
        ...
        ...         # Auto cleanup when context exits

    Args:
        session_name: Unique name for the Claude session. This is used by Claude to
                     identify and persist the session state on the filesystem.
        config: Claude session configuration
        workflow_instance: The workflow instance (must inherit from ClaudeMessageReceiver)
    """

    def __init__(
        self,
        session_name: str,
        config: ClaudeSessionConfig,
        workflow_instance: Any,
    ):
        """Initialize the managed session.

        Args:
            session_name: Unique name for Claude's persistent session
            config: Session configuration
            workflow_instance: Workflow that inherits from ClaudeMessageReceiver
        """
        self.session_name = session_name
        self.config = config
        self.workflow = workflow_instance
        self._context = None
        self._client = None
        self._is_paused = False

    async def __aenter__(self):
        """Start the session on context entry."""
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure cleanup on context exit."""
        if self._context:
            await self._disconnect()

    async def _connect(self):
        """Internal method to connect to Claude."""
        if not self._context:
            self._context = claude_workflow.claude_session(self.session_name, self.config)
            await self._context.__aenter__()
            self._client = SimplifiedClaudeClient(self.workflow)
            self._is_paused = False

    async def _disconnect(self):
        """Internal method to disconnect from Claude."""
        if self._context:
            if self._client:
                await self._client.close()
            await self._context.__aexit__(None, None, None)
            self._context = None
            self._client = None

    async def pause(self):
        """Temporarily disconnect to free Temporal worker resources.

        This closes the connection to Claude and stops the activity, but Claude's
        session state (conversation history, files, etc.) persists on the filesystem.
        The session can be resumed later with resume().

        This is useful for long-running workflows that have idle periods where
        Claude is not actively being used.
        """
        if self._context and not self._is_paused:
            await self._disconnect()
            self._is_paused = True

    async def resume(self):
        """Reconnect to Claude after pause.

        This creates a new activity and reconnects to the same Claude session.
        Claude automatically restores the session state from the filesystem,
        so the conversation history and any file changes are preserved.
        """
        if self._is_paused:
            await self._connect()
            self._is_paused = False

    async def send_query(self, prompt: str) -> str:
        """Send a query to Claude, auto-resuming if paused.

        Args:
            prompt: The query to send to Claude

        Returns:
            Claude's response text

        Raises:
            RuntimeError: If called after the context has exited
        """
        # Auto-resume if paused
        if self._is_paused:
            await self.resume()

        if not self._client:
            raise RuntimeError("Session not connected. Use within async with context.")

        # Collect response
        result = ""
        async for message in self._client.send_query(prompt):
            if message.get("type") == "assistant":
                msg_content = message.get("message", {}).get("content", [])
                for block in msg_content:
                    if block.get("type") == "text":
                        result += block.get("text", "")

        return result
