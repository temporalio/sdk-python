"""Initialize Temporal OpenAI Agents overrides."""

import dataclasses
import typing
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import AsyncIterator, Callable, Optional, Sequence, Union

from agents import ModelProvider, set_trace_provider
from agents.run import get_default_agent_runner, set_default_agent_runner
from agents.tracing import get_trace_provider
from agents.tracing.provider import DefaultTraceProvider

from temporalio.contrib.openai_agents._invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._openai_runner import (
    TemporalOpenAIRunner,
)
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsTracingInterceptor,
)
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError
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

if typing.TYPE_CHECKING:
    from temporalio.contrib.openai_agents import (
        StatefulMCPServerProvider,
        StatelessMCPServerProvider,
    )


@contextmanager
def set_open_ai_agent_temporal_overrides(
    model_params: ModelActivityParameters,
    auto_close_tracing_in_workflows: bool = False,
):
    """Configure Temporal-specific overrides for OpenAI agents.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments. Future versions may wrap the worker directly
        instead of requiring this context manager.

    This context manager sets up the necessary Temporal-specific runners and trace providers
    for running OpenAI agents within Temporal workflows. It should be called in the main
    entry point of your application before initializing the Temporal client and worker.

    The context manager handles:
    1. Setting up a Temporal-specific runner for OpenAI agents
    2. Configuring a Temporal-aware trace provider
    3. Restoring previous settings when the context exits

    Args:
        model_params: Configuration parameters for Temporal activity execution of model calls.
        auto_close_tracing_in_workflows: If set to true, close tracing spans immediately.

    Returns:
        A context manager that yields the configured TemporalTraceProvider.
    """
    previous_runner = get_default_agent_runner()
    previous_trace_provider = get_trace_provider()
    provider = TemporalTraceProvider(
        auto_close_in_workflows=auto_close_tracing_in_workflows
    )

    try:
        set_default_agent_runner(TemporalOpenAIRunner(model_params))
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_agent_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())


class OpenAIPayloadConverter(PydanticPayloadConverter):
    """PayloadConverter for OpenAI agents."""

    def __init__(self) -> None:
        """Initialize a payload converter."""
        super().__init__(ToJsonOptions(exclude_unset=True))


def _data_converter(converter: Optional[DataConverter]) -> DataConverter:
    if converter is None:
        return DataConverter(payload_converter_class=OpenAIPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=OpenAIPayloadConverter
        )
    elif not isinstance(converter.payload_converter, OpenAIPayloadConverter):
        raise ValueError(
            "The payload converter must be of type OpenAIPayloadConverter."
        )
    return converter


class OpenAIAgentsPlugin(SimplePlugin):
    """Temporal plugin for integrating OpenAI agents with Temporal workflows.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin provides seamless integration between the OpenAI Agents SDK and
    Temporal workflows. It automatically configures the necessary interceptors,
    activities, and data converters to enable OpenAI agents to run within
    Temporal workflows with proper tracing and model execution.

    The plugin:
    1. Configures the Pydantic data converter for type-safe serialization
    2. Sets up tracing interceptors for OpenAI agent interactions
    3. Registers model execution activities
    4. Automatically registers MCP server activities and manages their lifecycles
    5. Manages the OpenAI agent runtime overrides during worker execution

    Args:
        model_params: Configuration parameters for Temporal activity execution
            of model calls. If None, default parameters will be used.
        model_provider: Optional model provider for custom model implementations.
            Useful for testing or custom model integrations.
        mcp_server_providers: Sequence of MCP servers to automatically register with the worker.
            The plugin will wrap each server in a TemporalMCPServer if needed and
            manage their connection lifecycles tied to the worker lifetime. This is
            the recommended way to use MCP servers with Temporal workflows.

    Example:
        >>> from temporalio.client import Client
        >>> from temporalio.worker import Worker
        >>> from temporalio.contrib.openai_agents import OpenAIAgentsPlugin, ModelActivityParameters, StatelessMCPServerProvider
        >>> from agents.mcp import MCPServerStdio
        >>> from datetime import timedelta
        >>>
        >>> # Configure model parameters
        >>> model_params = ModelActivityParameters(
        ...     start_to_close_timeout=timedelta(seconds=30),
        ...     retry_policy=RetryPolicy(maximum_attempts=3)
        ... )
        >>>
        >>> # Create MCP servers
        >>> filesystem_server = StatelessMCPServerProvider(MCPServerStdio(
        ...     name="Filesystem Server",
        ...     params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}
        ... ))
        >>>
        >>> # Create plugin with MCP servers
        >>> plugin = OpenAIAgentsPlugin(
        ...     model_params=model_params,
        ...     mcp_server_providers=[filesystem_server]
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
        model_params: Optional[ModelActivityParameters] = None,
        model_provider: Optional[ModelProvider] = None,
        mcp_server_providers: Sequence[
            Union["StatelessMCPServerProvider", "StatefulMCPServerProvider"]
        ] = (),
        register_activities: bool = True,
    ) -> None:
        """Initialize the OpenAI agents plugin.

        Args:
            model_params: Configuration parameters for Temporal activity execution
                of model calls. If None, default parameters will be used.
            model_provider: Optional model provider for custom model implementations.
                Useful for testing or custom model integrations.
            mcp_server_providers: Sequence of MCP servers to automatically register with the worker.
                Each server will be wrapped in a TemporalMCPServer if not already wrapped,
                and their activities will be automatically registered with the worker.
                The plugin manages the connection lifecycle of these servers.
            register_activities: Whether to register activities during the worker execution.
                This can be disabled on some workers to allow a separation of workflows and activities
                but should not be disabled on all workers, or agents will not be able to progress.
        """
        if model_params is None:
            model_params = ModelActivityParameters()

        # For the default provider, we provide a default start_to_close_timeout of 60 seconds.
        # Other providers will need to define their own.
        if (
            model_params.start_to_close_timeout is None
            and model_params.schedule_to_close_timeout is None
        ):
            if model_provider is None:
                model_params.start_to_close_timeout = timedelta(seconds=60)
            else:
                raise ValueError(
                    "When configuring a custom provider, the model activity must have start_to_close_timeout or schedule_to_close_timeout"
                )

        # Delay activity construction until they are actually needed
        def add_activities(
            activities: Optional[Sequence[Callable]],
        ) -> Sequence[Callable]:
            if not register_activities:
                return activities or []

            new_activities = [ModelActivity(model_provider).invoke_model_activity]

            server_names = [server.name for server in mcp_server_providers]
            if len(server_names) != len(set(server_names)):
                raise ValueError(
                    f"More than one mcp server registered with the same name. Please provide unique names."
                )

            for mcp_server in mcp_server_providers:
                new_activities.extend(mcp_server._get_activities())
            return list(activities or []) + new_activities

        def workflow_runner(runner: Optional[WorkflowRunner]) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the OpenAI plugin.")

            # If in sandbox, add additional passthrough
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules("mcp"),
                )
            return runner

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            with set_open_ai_agent_temporal_overrides(model_params):
                yield

        super().__init__(
            name="OpenAIAgentsPlugin",
            data_converter=_data_converter,
            worker_interceptors=[OpenAIAgentsTracingInterceptor()],
            activities=add_activities,
            workflow_runner=workflow_runner,
            workflow_failure_exception_types=[AgentsWorkflowError],
            run_context=lambda: run_context(),
        )
