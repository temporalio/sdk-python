"""Support for using Temporal activities as OpenAI agents tools."""

import json
import typing
from datetime import timedelta
from typing import Any, Callable, Optional, Type

from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.nexus._util import get_operation_factory
from temporalio.workflow import ActivityCancellationType, VersioningIntent, unsafe

with unsafe.imports_passed_through():
    from agents import FunctionTool, RunContextWrapper, Tool
    from agents.function_schema import function_schema


class ToolSerializationError(TemporalError):
    """Error that occurs when a tool output could not be serialized."""


def activity_as_tool(
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
        result = await workflow.execute_activity(
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


def nexus_operation_as_tool(
    fn: Callable,
    *,
    service: Type[Any],
    endpoint: str,
    schedule_to_close_timeout: Optional[timedelta] = None,
) -> Tool:
    """Convert a Nexus operation into an OpenAI agent tool.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    This function takes a Nexus operation and converts it into an
    OpenAI agent tool that can be used by the agent to execute the operation
    during workflow execution. The tool will automatically handle the conversion
    of inputs and outputs between the agent and the operation.

    Args:
        fn: A Nexus operation to convert into a tool.
        service: The Nexus service class that contains the operation.
        endpoint: The Nexus endpoint to use for the operation.

    Returns:
        An OpenAI agent tool that wraps the provided operation.

    Raises:
        ApplicationError: If the operation is not properly decorated as a Nexus operation.

    Example:
        >>> @service_handler
        >>> class WeatherServiceHandler:
        ...     @sync_operation
        ...     async def get_weather_object(self, ctx: StartOperationContext, input: WeatherInput) -> Weather:
        ...         return Weather(
        ...             city=input.city, temperature_range="14-20C", conditions="Sunny with wind."
        ...         )
        >>>
        >>> # Create tool with custom activity options
        >>> tool = nexus_operation_as_tool(
        ...     WeatherServiceHandler.get_weather_object,
        ...     service=WeatherServiceHandler,
        ...     endpoint="weather-service",
        ... )
        >>> # Use tool with an OpenAI agent
    """
    if not get_operation_factory(fn):
        raise ApplicationError(
            "Function is not a Nexus operation",
            "invalid_tool",
        )

    schema = function_schema(adapt_nexus_operation_function_schema(fn))

    async def run_operation(ctx: RunContextWrapper[Any], input: str) -> Any:
        try:
            json_data = json.loads(input)
        except Exception as e:
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}"
            ) from e

        nexus_client = workflow.create_nexus_client(endpoint=endpoint, service=service)
        args, _ = schema.to_call_args(schema.params_pydantic_model(**json_data))
        assert len(args) == 1, "Nexus operations must have exactly one argument"
        [arg] = args
        result = await nexus_client.execute_operation(
            fn,
            arg,
            schedule_to_close_timeout=schedule_to_close_timeout,
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
        on_invoke_tool=run_operation,
        strict_json_schema=True,
    )


def adapt_nexus_operation_function_schema(fn: Callable[..., Any]) -> Callable[..., Any]:
    # Nexus operation start methods look like
    # async def operation(self, ctx: StartOperationContext, input: InputType) -> OutputType
    _, inputT, retT = typing.get_type_hints(fn).values()

    def adapted(input: inputT) -> retT:  # type: ignore
        pass

    adapted.__name__ = fn.__name__
    return adapted
