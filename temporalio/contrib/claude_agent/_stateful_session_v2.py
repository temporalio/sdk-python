"""Simplified stateful Claude Agent session implementation for Temporal workflows."""

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any, Optional

from temporalio import activity, workflow
from temporalio.exceptions import CancelledError
from temporalio.workflow import ActivityConfig, ActivityHandle

from ._session_config import ClaudeSessionConfig

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _SessionArguments:
    config: ClaudeSessionConfig
    caller_workflow_id: str
    caller_run_id: str


class _StatefulClaudeSessionReference(AbstractAsyncContextManager):
    """Reference to a stateful Claude session from within a workflow."""

    def __init__(
        self,
        session_name: str,
        config: ActivityConfig | None,
        session_config: ActivityConfig | None,
        claude_config: ClaudeSessionConfig,
    ):
        self._session_name = session_name
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1),
            schedule_to_start_timeout=timedelta(seconds=30),
        )
        # Session activity needs to run for the entire workflow duration
        self._session_config = session_config or ActivityConfig(
            start_to_close_timeout=timedelta(hours=24),  # Long timeout for session
            heartbeat_timeout=timedelta(seconds=60),  # Heartbeat to detect failures
        )
        self._connect_handle: ActivityHandle | None = None
        self._claude_config = claude_config

    async def connect(self) -> None:
        """Start the session activity."""
        # Get current workflow info to pass to the session
        current_info = workflow.info()

        # Start long-running activity that will manage Claude directly
        self._connect_handle = workflow.start_activity(
            self._session_name + "-session",
            _SessionArguments(
                config=self._claude_config,
                caller_workflow_id=current_info.workflow_id,
                caller_run_id=current_info.run_id,
            ),
            **self._session_config,
        )

    async def cleanup(self) -> None:
        """Clean up the session activity."""
        if self._connect_handle:
            # Cancel the activity first
            self._connect_handle.cancel()
            try:
                await self._connect_handle
            except Exception as e:
                from temporalio.exceptions import is_cancelled_exception

                if not is_cancelled_exception(e):
                    raise

    async def __aenter__(self):
        """Enter context manager."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """Exit context manager."""
        await self.cleanup()


class StatefulClaudeSessionProvider:
    """Simplified provider for stateful Claude Agent sessions in Temporal workflows.

    This class manages long-running Claude Code sessions by having the activity
    directly communicate with the workflow via queries and signals.

    The session maintains state across calls, including:
    - File system changes
    - Conversation history
    - Tool execution context
    """

    def __init__(self, name: str, default_options: Optional[Any] = None):
        """Initialize the stateful Claude session provider.

        Args:
            name: The name of the session (used for activity names)
            default_options: Default ClaudeAgentOptions to use for sessions (optional)
        """
        self._name = name
        self._default_options = default_options
        super().__init__()

    @property
    def name(self) -> str:
        """Get the session name."""
        return self._name

    def _get_activities(self) -> Sequence[Callable]:
        """Get the activities for this session provider."""

        async def heartbeat_every(delay: float, *details: Any) -> None:
            """Heartbeat every so often while not cancelled."""
            while True:
                await asyncio.sleep(delay)
                activity.heartbeat(*details)

        @activity.defn(name=self.name + "-session")
        async def session_activity(args: _SessionArguments) -> None:
            """Long-running activity that maintains the Claude Code session directly."""
            heartbeat_task = asyncio.create_task(heartbeat_every(30))

            # Import Claude SDK components here (outside workflow sandbox)
            from claude_agent_sdk._internal.query import Query
            from claude_agent_sdk._internal.transport.subprocess_cli import (
                SubprocessCLITransport,
            )

            transport = None
            query = None

            try:
                logger.info(f"Starting Claude session for workflow: {args.caller_workflow_id}")

                # Get handle to calling workflow
                client = activity.client()
                workflow_handle = client.get_workflow_handle(
                    args.caller_workflow_id,
                    run_id=args.caller_run_id
                )

                # Convert config to options
                options = args.config.to_claude_options()

                # Create a queue for messages from workflow to Claude
                message_queue = asyncio.Queue()
                session_active = True

                async def message_stream():
                    """Async generator that yields messages for Claude."""
                    while session_active:
                        try:
                            # Get next message with timeout
                            msg = await asyncio.wait_for(message_queue.get(), timeout=0.5)
                            if msg is None:  # Sentinel to stop
                                break
                            # Parse and yield the message
                            parsed = json.loads(msg.strip())
                            logger.debug(f"Yielding message to Claude: {parsed}")
                            yield parsed
                        except asyncio.TimeoutError:
                            # Check for new messages from workflow
                            continue
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse message: {msg} - {e}")

                # Create and connect transport with streaming mode
                logger.info("Creating SubprocessCLITransport in streaming mode")
                transport = SubprocessCLITransport(
                    prompt=message_stream(),
                    options=options,
                )
                await transport.connect()

                # Create and initialize Query
                logger.info("Creating Query in streaming mode")
                query = Query(
                    transport=transport,
                    is_streaming_mode=True,
                    can_use_tool=None,  # Callbacks can't cross boundaries
                    hooks=None,  # Hooks can't cross boundaries
                    sdk_mcp_servers={},
                )
                await query.start()
                await query.initialize()

                logger.info("Claude subprocess ready, entering main loop")

                # Start background task to read responses from Claude
                async def read_claude_responses():
                    """Read responses from Claude and send to workflow."""
                    try:
                        async for message in query.receive_messages():
                            # Send each message immediately to workflow
                            await workflow_handle.signal("receive_claude_message", message)
                            logger.debug(f"Sent to workflow: {message.get('type')}")
                    except Exception as e:
                        logger.exception(f"Error reading Claude responses: {e}")
                        # Send error to workflow
                        try:
                            await workflow_handle.signal(
                                "receive_claude_message",
                                {"type": "error", "error": str(e)}
                            )
                        except Exception:
                            pass

                response_task = asyncio.create_task(read_claude_responses())

                # Main loop to poll workflow for messages
                try:
                    while session_active:
                        # Poll workflow for outgoing messages
                        outgoing = await workflow_handle.query("get_outgoing_claude_messages")

                        # Check for session end signal
                        if outgoing:
                            logger.debug(f"Got {len(outgoing)} messages from workflow")
                            for msg in outgoing:
                                if msg == "END_SESSION":
                                    logger.info("Session end requested")
                                    session_active = False
                                    break
                                # Add message to queue for Claude
                                logger.debug(f"Queueing message for Claude: {msg[:100]}")
                                await message_queue.put(msg)

                        # Brief sleep to avoid tight polling
                        await asyncio.sleep(0.1)

                        # Check if response task has failed
                        if response_task.done():
                            exc = response_task.exception()
                            if exc:
                                raise exc
                            break

                except CancelledError:
                    logger.info("Session cancelled")
                    raise
                except Exception as e:
                    logger.exception(f"Error in session loop: {e}")
                    raise
                finally:
                    session_active = False
                    await message_queue.put(None)  # Stop the stream
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
                if query:
                    await query.close()
                if transport:
                    await transport.close()

        return (session_activity,)