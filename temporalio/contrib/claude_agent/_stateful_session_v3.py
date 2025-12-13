"""Stateful session implementation for Claude Agent SDK - Version 3 with multi-turn support."""

import asyncio
import json
import logging
from asyncio import CancelledError
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any, Optional

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError
from temporalio.workflow import ActivityConfig, ActivityHandle

from ._session_config import ClaudeSessionConfig

logger = logging.getLogger(__name__)


class ClaudeSessionArgs(BaseModel):
    """Arguments for the Claude session activity."""

    caller_workflow_id: str
    caller_run_id: str | None
    config: ClaudeSessionConfig


class _StatefulClaudeSessionReference(AbstractAsyncContextManager):
    """Context manager for a stateful Claude session in workflow."""

    def __init__(
        self,
        name: str,
        config: ActivityConfig | None,
        session_config: ClaudeSessionConfig,
        original_claude_config: dict[str, Any] | None,
    ):
        """Initialize the session reference.

        Args:
            name: Name of the session
            config: Activity configuration
            session_config: Configuration for Claude
            original_claude_config: Original ClaudeAgentOptions if available
        """
        self._name = name
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._session_config = session_config
        self._original_config = original_claude_config
        self._activity_handle: Optional[ActivityHandle] = None

    async def __aenter__(self) -> Any:
        """Enter the context and start the session activity."""
        # Get current workflow info
        info = workflow.info()

        # Create session arguments
        args = ClaudeSessionArgs(
            caller_workflow_id=info.workflow_id,
            caller_run_id=info.run_id,
            config=self._session_config,
        )

        # Start the session activity
        self._activity_handle = workflow.start_activity(
            "claude-session-activity",
            args,
            **self._config,
        )

        # Wait briefly for activity to initialize
        await asyncio.sleep(0.5)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the context and cancel the session activity."""
        if self._activity_handle:
            self._activity_handle.cancel()
            try:
                await self._activity_handle
            except Exception:
                pass  # Activity cancelled, expected


@activity.defn(name="claude-session-activity")
async def session_activity(args: ClaudeSessionArgs) -> None:
    """Activity that manages a stateful Claude session with multi-turn support."""
    async def heartbeat_every(delay: float):
        """Send heartbeat regularly."""
        while True:
            await asyncio.sleep(delay)
            activity.heartbeat()

    heartbeat_task = asyncio.create_task(heartbeat_every(30))

    # Import Claude SDK components here (outside workflow sandbox)
    from claude_agent_sdk import ClaudeSDKClient

    client = None

    try:
        logger.info(f"Starting Claude session for workflow: {args.caller_workflow_id}")

        # Get handle to calling workflow
        temporal_client = activity.client()
        workflow_handle = temporal_client.get_workflow_handle(
            args.caller_workflow_id,
            run_id=args.caller_run_id
        )

        # Convert config to options
        options = args.config.to_claude_options()

        # Create and connect Claude SDK client
        logger.info("Creating ClaudeSDKClient")
        client = ClaudeSDKClient(options)
        await client.connect()

        logger.info("Claude client connected, entering main loop")

        # Start background task to read responses
        async def read_responses():
            try:
                logger.info("Starting to read responses from Claude")
                async for message in client.receive_messages():
                    # Convert message to dict format for serialization
                    msg_type = type(message).__name__
                    logger.debug(f"Received from Claude: {msg_type}")

                    if msg_type == "SystemMessage":
                        response = {
                            "type": "system",
                            "data": message.data if hasattr(message, "data") else {}
                        }
                    elif msg_type == "AssistantMessage":
                        # Extract text from content blocks
                        text = ""
                        for block in message.content:
                            if hasattr(block, "text"):
                                text += block.text
                        response = {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": text}]
                            }
                        }
                    elif msg_type == "ResultMessage":
                        response = {
                            "type": "result",
                            "result": message.result if hasattr(message, "result") else "",
                            "duration_ms": message.duration_ms if hasattr(message, "duration_ms") else 0
                        }
                    else:
                        # Other message types
                        response = {"type": msg_type.lower().replace("message", "")}

                    await workflow_handle.signal("receive_claude_message", response)
                    logger.debug(f"Sent to workflow: {response.get('type')}")

            except Exception as e:
                logger.exception(f"Error reading responses: {e}")
                await workflow_handle.signal(
                    "receive_claude_message",
                    {"type": "error", "error": str(e)}
                )

        response_task = asyncio.create_task(read_responses())

        # Main session loop - handle multiple queries
        session_active = True
        try:
            while session_active:
                # Poll workflow for outgoing messages
                outgoing = await workflow_handle.query("get_outgoing_claude_messages")

                if not outgoing:
                    # No messages yet, wait briefly
                    await asyncio.sleep(0.1)
                    continue

                # Check for session end signal
                if "END_SESSION" in outgoing:
                    logger.info("Session end requested")
                    break

                # Process each message
                for msg_str in outgoing:
                    try:
                        # Parse the message from SimplifiedClaudeClient
                        message = json.loads(msg_str.strip())

                        # Extract content from the nested structure
                        content = ""
                        if "message" in message and "content" in message["message"]:
                            content = message["message"]["content"]

                        logger.info(f"Processing query: {content[:100]}...")

                        # Send query to Claude using SDK client
                        await client.query(content)
                        logger.info("Query sent successfully")

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse message: {msg_str} - {e}")
                        await workflow_handle.signal(
                            "receive_claude_message",
                            {"type": "error", "error": f"Invalid message format: {e}"}
                        )
                    except Exception as e:
                        logger.exception(f"Error processing query: {e}")
                        await workflow_handle.signal(
                            "receive_claude_message",
                            {"type": "error", "error": str(e)}
                        )

        except CancelledError:
            logger.info("Session cancelled")
            raise
        except Exception as e:
            logger.exception(f"Error in session loop: {e}")
            raise
        finally:
            # Stop the response task
            response_task.cancel()
            try:
                await response_task
            except asyncio.CancelledError:
                pass

    except CancelledError:
        logger.info(f"Session activity cancelled for: {args.caller_workflow_id}")
        raise
    except Exception as e:
        logger.exception(f"Error in session activity: {e}")
        raise
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        # Cleanup Claude resources
        if client:
            await client.disconnect()


class StatefulClaudeSessionProvider:
    """Provider for stateful Claude sessions in Temporal workflows."""

    def __init__(self, name: str):
        """Initialize the session provider.

        Args:
            name: Name of the session
        """
        self._name = name

    @property
    def name(self) -> str:
        """Get the session name."""
        return self._name

    def _get_activities(self) -> tuple:
        """Get activities for this provider."""
        return self.activities

    def create_session(
        self,
        config: ActivityConfig | None,
        session_config: ClaudeSessionConfig,
        original_config: dict[str, Any] | None,
    ) -> _StatefulClaudeSessionReference:
        """Create a new session reference.

        Args:
            config: Activity configuration
            session_config: Configuration for Claude
            original_config: Original ClaudeAgentOptions if available

        Returns:
            A session reference that can be used as a context manager
        """
        return _StatefulClaudeSessionReference(
            self._name, config, session_config, original_config
        )

    @property
    def activities(self) -> tuple:
        """Get the activities for this session provider.

        Returns:
            Tuple containing the session activity
        """
        return (session_activity,)