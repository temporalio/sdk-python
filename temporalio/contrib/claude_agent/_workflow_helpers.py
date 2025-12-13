"""Helper classes and mixins for workflows using Claude Agent SDK."""

import asyncio
from typing import Any

from temporalio import workflow


class ClaudeMessageReceiver:
    """Mixin for workflows that need to communicate with Claude sessions.

    This class provides signal handlers, query methods, and message buffers needed
    for bidirectional communication with Claude sessions. Workflows should inherit
    from this class to gain the ability to send and receive Claude messages.

    Example:
        >>> from temporalio import workflow
        >>> from temporalio.contrib.claude_agent import ClaudeMessageReceiver
        >>>
        >>> @workflow.defn
        >>> class MyWorkflow(ClaudeMessageReceiver):
        ...     @workflow.run
        ...     async def run(self) -> str:
        ...         # Initialize the receiver
        ...         self.init_claude_receiver()
        ...         # Send messages via self.send_to_claude()
        ...         # Receive messages via signals and wait_for_claude_messages()
    """

    def init_claude_receiver(self) -> None:
        """Initialize the Claude message receiver.

        Must be called in the workflow's run method before using Claude sessions.
        """
        self._claude_messages: list[dict[str, Any]] = []
        self._claude_outgoing: list[str] = []
        self._claude_message_event = asyncio.Event()

    @workflow.signal
    async def receive_claude_message(self, message: dict[str, Any]) -> None:
        """Receive a message from the Claude session activity.

        This signal handler is called by the session activity when messages
        are received from Claude.

        Args:
            message: The message received from Claude
        """
        if not hasattr(self, "_claude_messages"):
            # Initialize if not already done
            self.init_claude_receiver()

        workflow.logger.debug(f"Received Claude message: {message.get('type')}")
        self._claude_messages.append(message)
        self._claude_message_event.set()

    async def wait_for_claude_messages(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Wait for and retrieve Claude messages.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            List of messages received (may be empty if timeout)
        """
        if not hasattr(self, "_claude_messages"):
            self.init_claude_receiver()

        if timeout is not None:
            try:
                await asyncio.wait_for(self._claude_message_event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        else:
            await self._claude_message_event.wait()

        # Get and clear messages
        messages = self._claude_messages
        self._claude_messages = []
        self._claude_message_event.clear()
        return messages

    def get_claude_messages(self) -> list[dict[str, Any]]:
        """Get any buffered Claude messages without waiting.

        Returns:
            List of buffered messages (may be empty)
        """
        if not hasattr(self, "_claude_messages"):
            self.init_claude_receiver()

        messages = self._claude_messages
        self._claude_messages = []
        self._claude_message_event.clear()
        return messages

    @workflow.query
    def get_outgoing_claude_messages(self) -> list[str]:
        """Get outgoing messages for Claude (called by session activity).

        Returns:
            List of messages to send to Claude
        """
        if not hasattr(self, "_claude_outgoing"):
            self.init_claude_receiver()

        messages = self._claude_outgoing
        self._claude_outgoing = []
        return messages

    def send_to_claude(self, message: str) -> None:
        """Queue a message to send to Claude.

        Args:
            message: Raw message to send (typically JSON + newline)
        """
        if not hasattr(self, "_claude_outgoing"):
            self.init_claude_receiver()

        self._claude_outgoing.append(message)