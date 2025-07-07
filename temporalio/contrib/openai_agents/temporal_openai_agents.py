"""Initialize Temporal OpenAI Agents overrides."""

from contextlib import contextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Optional, Union, overload

from agents.items import TResponseStreamEvent
from openai.types.responses import ResponsePromptParam

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.contrib.openai_agents.model_parameters import ModelActivityParameters
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType, VersioningIntent, unsafe

with unsafe.imports_passed_through():
    from agents import (
        Agent,
        AgentOutputSchemaBase,
        FunctionTool,
        Handoff,
        Model,
        ModelProvider,
        ModelResponse,
        ModelSettings,
        ModelTracing,
        OpenAIResponsesModel,
        RunContextWrapper,
        Tool,
        TResponseInputItem,
        set_trace_provider,
    )
    from agents.function_schema import DocstringStyle, function_schema
    from agents.run import get_default_agent_runner, set_default_agent_runner
    from agents.tool import (
        ToolErrorFunction,
        ToolFunction,
        default_tool_error_function,
        function_tool,
    )
    from agents.tracing import get_trace_provider
    from agents.tracing.provider import DefaultTraceProvider
    from agents.util._types import MaybeAwaitable


@contextmanager
def set_open_ai_agent_temporal_overrides(
    model_params: ModelActivityParameters,
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

    Returns:
        A context manager that yields the configured TemporalTraceProvider.

    """
    if (
        not model_params.start_to_close_timeout
        and not model_params.schedule_to_close_timeout
    ):
        raise ValueError(
            "Activity must have start_to_close_timeout or schedule_to_close_timeout"
        )

    previous_runner = get_default_agent_runner()
    previous_trace_provider = get_trace_provider()
    provider = TemporalTraceProvider()

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
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Get a streamed response from the model. Unimplemented."""
        raise NotImplementedError()


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
        of inputs and outputs between the agent and the activity.

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

        async def run_activity(ctx: RunContextWrapper[Any], input: str) -> Any:
            try:
                return str(
                    await temporal_workflow.execute_activity(
                        fn,
                        input,
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
                )
            except Exception:
                raise ApplicationError(
                    "You must return a string representation of the tool output, or something we can call str() on"
                )

        schema = function_schema(fn)
        tool = FunctionTool(
            name=schema.name,
            description=schema.description or "",
            params_json_schema=schema.params_json_schema,
            on_invoke_tool=run_activity,
            strict_json_schema=True,
        )
        setattr(tool, "__temporal_tool_definition", True)
        return tool

    @classmethod
    @overload
    def tool(
        cls,
        *,
        name_override: Union[str, None] = None,
        description_override: Union[str, None] = None,
        docstring_style: Union[DocstringStyle, None] = None,
        use_docstring_info: bool = True,
        failure_error_function: Union[
            ToolErrorFunction, None
        ] = default_tool_error_function,
        strict_mode: bool = True,
        is_enabled: Union[
            bool, Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]]
        ] = True,
    ) -> Callable[[ToolFunction[...]], FunctionTool]: ...

    @classmethod
    @overload
    def tool(
        cls,
        func: ToolFunction[...],
        *,
        name_override: Union[str, None] = None,
        description_override: Union[str, None] = None,
        docstring_style: Union[DocstringStyle, None] = None,
        use_docstring_info: bool = True,
        failure_error_function: Union[
            ToolErrorFunction, None
        ] = default_tool_error_function,
        strict_mode: bool = True,
        is_enabled: Union[
            bool, Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]]
        ] = True,
    ) -> FunctionTool: ...

    @classmethod
    def tool(
        cls,
        func: Union[ToolFunction[...], None] = None,
        *,
        name_override: Union[str, None] = None,
        description_override: Union[str, None] = None,
        docstring_style: Union[DocstringStyle, None] = None,
        use_docstring_info: bool = True,
        failure_error_function: Union[
            ToolErrorFunction, None
        ] = default_tool_error_function,
        strict_mode: bool = True,
        is_enabled: Union[
            bool, Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]]
        ] = True,
    ) -> Union[FunctionTool, Callable[[ToolFunction[...]], FunctionTool]]:
        """A temporal specific wrapper for OpenAI's @function_tool. This exists to ensure the user is aware that the function tool is workflow level code and must be deterministic."""
        tool = function_tool(
            func,  # type: ignore
            name_override=name_override,
            description_override=description_override,
            docstring_style=docstring_style,
            use_docstring_info=use_docstring_info,
            failure_error_function=failure_error_function,
            strict_mode=strict_mode,
            is_enabled=is_enabled,
        )
        setattr(tool, "__temporal_tool_definition", True)
        return tool
