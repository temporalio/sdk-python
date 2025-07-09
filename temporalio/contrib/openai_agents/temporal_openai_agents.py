"""Initialize Temporal OpenAI Agents overrides."""

import json
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Optional, Union, overload

from agents import (
    Agent,
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    RunContextWrapper,
    Tool,
    TResponseInputItem,
    set_trace_provider,
)
from agents.function_schema import DocstringStyle, function_schema
from agents.items import TResponseStreamEvent
from agents.run import get_default_agent_runner, set_default_agent_runner
from agents.tool import (
    FunctionTool,
    ToolErrorFunction,
    ToolFunction,
    ToolParams,
    default_tool_error_function,
    function_tool,
)
from agents.tracing import get_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from agents.util._types import MaybeAwaitable
from openai.types.responses import ResponsePromptParam

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@contextmanager
def set_open_ai_agent_temporal_overrides(
    model_params: Optional[ModelActivityParameters] = None,
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
    if model_params is None:
        model_params = ModelActivityParameters()

    if (
        not model_params.start_to_close_timeout
        and not model_params.schedule_to_close_timeout
    ):
        raise ValueError(
            "Activity must have start_to_close_timeout or schedule_to_close_timeout"
        )

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


class TestModelProvider(ModelProvider):
    """Test model provider which simply returns the given module."""

    def __init__(self, model: Model):
        """Initialize a test model provider with a model."""
        self._model = model

    def get_model(self, model_name: Union[str, None]) -> Model:
        """Get a model from the model provider."""
        return self._model


class TestModel(Model):
    """Test model for use mocking model responses."""

    def __init__(self, fn: Callable[[], ModelResponse]) -> None:
        """Initialize a test model with a callable."""
        self.fn = fn

    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Union[AgentOutputSchemaBase, None],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Union[str, None],
        prompt: Union[ResponsePromptParam, None] = None,
    ) -> ModelResponse:
        """Get a response from the model."""
        return self.fn()

    def stream_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Optional[str],
        prompt: Optional[ResponsePromptParam],
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Get a streamed response from the model. Unimplemented."""
        raise NotImplementedError()


class ToolSerializationError(TemporalError):
    """Error that occurs when a tool output could not be serialized."""


class workflow:
    """Encapsulates workflow specific primitives for working with the OpenAI Agents SDK in a workflow context"""

    @classmethod
    def activity_as_tool(
        cls,
        fn: Callable,
        *,
        task_queue: Optional[str] = None,
        schedule_to_close_timeout: Optional[timedelta] = None,
        schedule_to_start_timeout: Optional[timedelta] = None,
        start_to_close_timeout: Optional[timedelta] = None,
        heartbeat_timeout: Optional[timedelta] = None,
        retry_policy: Optional[RetryPolicy] = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        activity_id: Optional[str] = None,
        versioning_intent: Optional[VersioningIntent] = None,
        summary: Optional[str] = None,
        priority: Priority = Priority.default,
    ) -> Tool:
        """Convert a single Temporal activity function to an OpenAI agent tool.

        .. warning::
            This API is experimental and may change in future versions.
            Use with caution in production environments.

        This function takes a Temporal activity function and converts it into an
        OpenAI agent tool that can be used by the agent to execute the activity
        during workflow execution. The tool will automatically handle the conversion
        of inputs and outputs between the agent and the activity. Note that if you take a context,
        mutation will not be persisted, as the activity may not be running in the same location.

        Args:
            fn: A Temporal activity function to convert to a tool.
            For other arguments, refer to :py:mod:`workflow` :py:meth:`start_activity`

        Returns:
            An OpenAI agent tool that wraps the provided activity.

        Raises:
            ApplicationError: If the function is not properly decorated as a Temporal activity.

        Example:
            >>> @activity.defn
            >>> def process_data(input: str) -> str:
            ...     return f"Processed: {input}"
            >>>
            >>> # Create tool with custom activity options
            >>> tool = activity_as_tool(
            ...     process_data,
            ...     start_to_close_timeout=timedelta(seconds=30),
            ...     retry_policy=RetryPolicy(maximum_attempts=3),
            ...     heartbeat_timeout=timedelta(seconds=10)
            ... )
            >>> # Use tool with an OpenAI agent
        """
        ret = activity._Definition.from_callable(fn)
        if not ret:
            raise ApplicationError(
                "Bare function without tool and activity decorators is not supported",
                "invalid_tool",
            )
        schema = function_schema(fn)

        async def run_activity(ctx: RunContextWrapper[Any], input: str) -> Any:
            try:
                json_data = json.loads(input)
            except Exception as e:
                raise ApplicationError(
                    f"Invalid JSON input for tool {schema.name}: {input}"
                ) from e

            # Activities don't support keyword only arguments, so we can ignore the kwargs_dict return
            args, _ = schema.to_call_args(schema.params_pydantic_model(**json_data))

            # Add the context to the arguments if it takes that
            if schema.takes_context:
                args = [ctx] + args

            result = await temporal_workflow.execute_activity(
                fn,
                args=args,
                task_queue=task_queue,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                cancellation_type=cancellation_type,
                activity_id=activity_id,
                versioning_intent=versioning_intent,
                summary=summary,
                priority=priority,
            )
            try:
                return str(result)
            except Exception as e:
                raise ToolSerializationError(
                    "You must return a string representation of the tool output, or something we can call str() on"
                ) from e

        return FunctionTool(
            name=schema.name,
            description=schema.description or "",
            params_json_schema=schema.params_json_schema,
            on_invoke_tool=run_activity,
            strict_json_schema=True,
        )
