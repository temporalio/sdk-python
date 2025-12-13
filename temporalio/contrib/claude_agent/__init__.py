"""Support for using the Claude Agent SDK as part of Temporal workflows.

This module provides compatibility between the
`Claude Agent SDK <https://github.com/anthropics/claude-agent-sdk-python>`_ and Temporal workflows.

Key Features:
    - **Stateful Sessions**: Maintain conversation context across workflow executions
    - **Multi-turn Conversations**: Support for extended dialogues with Claude
    - **Workflow-Safe**: All Claude SDK operations run in activities outside the workflow sandbox
    - **Tool Support**: Configure allowed/disallowed tools for Claude to use
    - **Automatic Serialization**: Built-in Pydantic data converters for type-safe message passing

Quick Start:
    >>> from temporalio import workflow
    >>> from temporalio.contrib.claude_agent import (
    ...     ClaudeMessageReceiver, ClaudeSessionConfig, SimplifiedClaudeClient,
    ...     workflow as claude_workflow
    ... )
    >>>
    >>> @workflow.defn
    >>> class MyWorkflow(ClaudeMessageReceiver):
    ...     @workflow.run
    ...     async def run(self, prompt: str) -> str:
    ...         self.init_claude_receiver()
    ...         config = ClaudeSessionConfig(system_prompt="Be helpful")
    ...
    ...         async with claude_workflow.claude_session("session", config):
    ...             client = SimplifiedClaudeClient(self)
    ...             async for msg in client.send_query(prompt):
    ...                 # Process messages
    ...                 pass

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

import dataclasses
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager

from temporalio.contrib.claude_agent._session_config import ClaudeSessionConfig
from temporalio.contrib.claude_agent._simple_client import SimplifiedClaudeClient
from temporalio.contrib.claude_agent._stateful_session_v3 import (
    StatefulClaudeSessionProvider,
)
from temporalio.contrib.claude_agent._workflow_helpers import ClaudeMessageReceiver
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter,
    ToJsonOptions,
)
from temporalio.converter import (
    DataConverter,
    DefaultPayloadConverter,
)
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from . import workflow

__all__ = [
    "ClaudeAgentPlugin",
    "ClaudeMessageReceiver",
    "ClaudeSessionConfig",
    "SimplifiedClaudeClient",
    "StatefulClaudeSessionProvider",
    "workflow",
]


class ClaudeAgentPayloadConverter(PydanticPayloadConverter):
    """PayloadConverter for Claude Agent SDK."""

    def __init__(self) -> None:
        """Initialize a payload converter."""
        super().__init__(ToJsonOptions(exclude_unset=True))


def _data_converter(converter: DataConverter | None) -> DataConverter:
    """Create or modify data converter for Claude Agent SDK."""
    if converter is None:
        return DataConverter(payload_converter_class=ClaudeAgentPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=ClaudeAgentPayloadConverter
        )
    elif not isinstance(converter.payload_converter, ClaudeAgentPayloadConverter):
        raise ValueError(
            "The payload converter must be of type ClaudeAgentPayloadConverter."
        )
    return converter


class ClaudeAgentPlugin(SimplePlugin):
    """Temporal plugin for integrating Claude Agent SDK with Temporal workflows.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin provides seamless integration between the Claude Agent SDK and
    Temporal workflows. It automatically configures the necessary activities,
    data converters, and sandbox restrictions to enable Claude Code to run
    within Temporal workflows with proper state management.

    The plugin:
    1. Configures the Pydantic data converter for type-safe serialization
    2. Registers Claude session activities for stateful communication
    3. Manages Claude Code session lifecycles tied to workflow runs
    4. Adds necessary sandbox passthroughs for Claude Agent SDK modules

    Args:
        session_providers: Sequence of Claude session providers to register with the worker.
            Each provider manages a named Claude Code session.
        register_activities: Whether to register activities during worker execution.
            This can be disabled on some workers to allow separation of workflows and activities
            but should not be disabled on all workers.

    Example:
        >>> from temporalio.client import Client
        >>> from temporalio.worker import Worker
        >>> from temporalio.contrib.claude_agent import ClaudeAgentPlugin, StatefulClaudeSessionProvider
        >>>
        >>> # Create session provider
        >>> session_provider = StatefulClaudeSessionProvider("my-session")
        >>>
        >>> # Create plugin with session provider
        >>> plugin = ClaudeAgentPlugin(
        ...     session_providers=[session_provider]
        ... )
        >>>
        >>> # Use with client and worker
        >>> client = await Client.connect(
        ...     "localhost:7233",
        ...     plugins=[plugin]
        ... )
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ... )
    """

    def __init__(
        self,
        session_providers: Sequence[StatefulClaudeSessionProvider] = (),
        register_activities: bool = True,
    ) -> None:
        """Initialize the Claude Agent SDK plugin.

        Args:
            session_providers: Sequence of session providers to register
            register_activities: Whether to register activities
        """
        # Check for duplicate session names early
        session_names = [provider.name for provider in session_providers]
        if len(session_names) != len(set(session_names)):
            raise ValueError(
                "More than one session provider registered with the same name. "
                "Please provide unique names."
            )

        def add_activities(
            activities: Sequence[Callable] | None,
        ) -> Sequence[Callable]:
            if not register_activities:
                return activities or []

            new_activities = []

            # Register activities from each session provider
            for provider in session_providers:
                new_activities.extend(provider._get_activities())

            return list(activities or []) + new_activities

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the Claude Agent plugin.")

            # If in sandbox, add passthrough for claude_agent_sdk modules and dependencies
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "claude_agent_sdk", "anyio", "sniffio"
                    ),
                )
            return runner

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            # No special run context needed currently
            yield

        super().__init__(
            name="ClaudeAgentPlugin",
            data_converter=_data_converter,
            activities=add_activities,
            workflow_runner=workflow_runner,
            run_context=lambda: run_context(),
        )
