"""Simplified Claude client for workflows."""

import json
from typing import Any, AsyncIterator


class SimplifiedClaudeClient:
    """Simple client for Claude communication in workflows.

    This client works with the ClaudeMessageReceiver mixin to send queries
    to Claude via the session activity. All actual Claude SDK operations
    happen in the activity, outside the workflow sandbox.

    Supports multi-turn conversations within a single session.

    Example:
        >>> # In a workflow that inherits from ClaudeMessageReceiver
        >>> client = SimplifiedClaudeClient(self)
        >>>
        >>> # Send first query
        >>> async for message in client.send_query("Hello Claude"):
        ...     if message.get("type") == "assistant":
        ...         print(message)
        >>>
        >>> # Send follow-up (Claude remembers context)
        >>> async for message in client.send_query("What did I just ask?"):
        ...     if message.get("type") == "assistant":
        ...         print(message)
        >>>
        >>> # Close when done
        >>> await client.close()

    Note:
        The workflow must inherit from ClaudeMessageReceiver and call
        init_claude_receiver() before using this client.
    """

    def __init__(self, workflow_instance: Any):
        """Initialize the client.

        Args:
            workflow_instance: The workflow instance (must have ClaudeMessageReceiver methods)
        """
        self._workflow = workflow_instance
        if not hasattr(self._workflow, "send_to_claude"):
            raise ValueError("Workflow must inherit from ClaudeMessageReceiver")
        self._session_active = True

    async def send_query(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        """Send a query to Claude and receive responses.

        Can be called multiple times for multi-turn conversations.

        Args:
            prompt: The prompt to send to Claude

        Yields:
            Response messages from Claude
        """
        if not self._session_active:
            raise RuntimeError("Session has been closed")

        # Construct the user message in the format expected by Claude SDK
        # This matches the format used by ClaudeSDKClient.query()
        user_message = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": "default"
        }

        # Send the message
        msg = json.dumps(user_message) + "\n"
        self._workflow.send_to_claude(msg)

        # Wait for and yield responses for this specific query
        assistant_received = False
        while not assistant_received:
            # Wait for messages with timeout
            messages = await self._workflow.wait_for_claude_messages(timeout=10.0)

            for message in messages:
                yield message

                # Check if we've received the assistant's response
                if message.get("type") == "assistant":
                    assistant_received = True
                    # Don't break - yield all messages from this batch
                elif message.get("type") in ("error", "end"):
                    # Error or end indicates session should close
                    self._session_active = False
                    assistant_received = True
                    break

            # If no messages after timeout, check if there are any buffered
            if not messages:
                buffered = self._workflow.get_claude_messages()
                for message in buffered:
                    yield message
                    if message.get("type") == "assistant":
                        assistant_received = True
                    elif message.get("type") in ("error", "end"):
                        self._session_active = False
                        assistant_received = True
                        break

    async def close(self) -> None:
        """Close the Claude session.

        This should be called when done with the conversation to clean up resources.
        """
        if self._session_active:
            self._workflow.send_to_claude("END_SESSION")
            self._session_active = False