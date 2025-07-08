"""Support for using Temporal activities as OpenAI agents tools."""

from __future__ import annotations

import inspect
import json
from datetime import timedelta
from typing import Any, Callable, Optional, Type, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, create_model

from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.nexus._util import get_operation_factory
from temporalio.workflow import ActivityCancellationType, VersioningIntent, unsafe

with unsafe.imports_passed_through():
    from agents import FunctionTool, RunContextWrapper, Tool
    from agents.function_schema import (
        FuncSchema,
        ToolContext,
        ensure_strict_json_schema,
        generate_func_documentation,
    )


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
    fn: Callable[..., Any],
    *,
    service: Type[Any],
    endpoint: str,
    schedule_to_close_timeout: Optional[timedelta] = None,
    function_schema_globalns: Optional[dict[str, Any]] = None,
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

    schema = function_schema(
        adapt_nexus_operation_function_schema(fn),
        globalns=function_schema_globalns,
    )

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
    _, input_type, ret_type = fn.__annotations__.values()

    def adapted(input):
        pass

    adapted.__annotations__ = {"input": input_type, "return": ret_type}
    return adapted


def function_schema(
    func: Callable[..., Any],
    docstring_style: DocstringStyle | None = None,
    name_override: str | None = None,
    description_override: str | None = None,
    use_docstring_info: bool = True,
    strict_json_schema: bool = True,
    globalns: Optional[dict[str, Any]] = None,
) -> FuncSchema:
    """
    Given a python function, extracts a `FuncSchema` from it, capturing the name, description,
    parameter descriptions, and other metadata.

    Args:
        func: The function to extract the schema from.
        docstring_style: The style of the docstring to use for parsing. If not provided, we will
            attempt to auto-detect the style.
        name_override: If provided, use this name instead of the function's `__name__`.
        description_override: If provided, use this description instead of the one derived from the
            docstring.
        use_docstring_info: If True, uses the docstring to generate the description and parameter
            descriptions.
        strict_json_schema: Whether the JSON schema is in strict mode. If True, we'll ensure that
            the schema adheres to the "strict" standard the OpenAI API expects. We **strongly**
            recommend setting this to True, as it increases the likelihood of the LLM providing
            correct JSON input.

    Returns:
        A `FuncSchema` object containing the function's name, description, parameter descriptions,
        and other metadata.
    """

    # 1. Grab docstring info
    if use_docstring_info:
        doc_info = generate_func_documentation(func, docstring_style)
        param_descs = doc_info.param_descriptions or {}
    else:
        doc_info = None
        param_descs = {}

    # Ensure name_override takes precedence even if docstring info is disabled.
    func_name = name_override or (doc_info.name if doc_info else func.__name__)

    # 2. Inspect function signature and get type hints
    sig = inspect.signature(func)
    type_hints = get_type_hints(func, globalns=globalns)
    params = list(sig.parameters.items())
    takes_context = False
    filtered_params = []

    if params:
        first_name, first_param = params[0]
        # Prefer the evaluated type hint if available
        ann = type_hints.get(first_name, first_param.annotation)
        if ann != inspect._empty:
            origin = get_origin(ann) or ann
            if origin is RunContextWrapper or origin is ToolContext:
                takes_context = True  # Mark that the function takes context
            else:
                filtered_params.append((first_name, first_param))
        else:
            filtered_params.append((first_name, first_param))

    # For parameters other than the first, raise error if any use RunContextWrapper or ToolContext.
    for name, param in params[1:]:
        ann = type_hints.get(name, param.annotation)
        if ann != inspect._empty:
            origin = get_origin(ann) or ann
            if origin is RunContextWrapper or origin is ToolContext:
                raise UserError(
                    f"RunContextWrapper/ToolContext param found at non-first position in function"
                    f" {func.__name__}"
                )
        filtered_params.append((name, param))

    # We will collect field definitions for create_model as a dict:
    #   field_name -> (type_annotation, default_value_or_Field(...))
    fields: dict[str, Any] = {}

    for name, param in filtered_params:
        ann = type_hints.get(name, param.annotation)
        default = param.default

        # If there's no type hint, assume `Any`
        if ann == inspect._empty:
            ann = Any

        # If a docstring param description exists, use it
        field_description = param_descs.get(name, None)

        # Handle different parameter kinds
        if param.kind == param.VAR_POSITIONAL:
            # e.g. *args: extend positional args
            if get_origin(ann) is tuple:
                # e.g. def foo(*args: tuple[int, ...]) -> treat as List[int]
                args_of_tuple = get_args(ann)
                if len(args_of_tuple) == 2 and args_of_tuple[1] is Ellipsis:
                    ann = list[args_of_tuple[0]]  # type: ignore
                else:
                    ann = list[Any]
            else:
                # If user wrote *args: int, treat as List[int]
                ann = list[ann]  # type: ignore

            # Default factory to empty list
            fields[name] = (
                ann,
                Field(default_factory=list, description=field_description),  # type: ignore
            )

        elif param.kind == param.VAR_KEYWORD:
            # **kwargs handling
            if get_origin(ann) is dict:
                # e.g. def foo(**kwargs: dict[str, int])
                dict_args = get_args(ann)
                if len(dict_args) == 2:
                    ann = dict[dict_args[0], dict_args[1]]  # type: ignore
                else:
                    ann = dict[str, Any]
            else:
                # e.g. def foo(**kwargs: int) -> Dict[str, int]
                ann = dict[str, ann]  # type: ignore

            fields[name] = (
                ann,
                Field(default_factory=dict, description=field_description),  # type: ignore
            )

        else:
            # Normal parameter
            if default == inspect._empty:
                # Required field
                fields[name] = (
                    ann,
                    Field(..., description=field_description),
                )
            else:
                # Parameter with a default value
                fields[name] = (
                    ann,
                    Field(default=default, description=field_description),
                )

    # 3. Dynamically build a Pydantic model
    dynamic_model = create_model(f"{func_name}_args", __base__=BaseModel, **fields)

    # 4. Build JSON schema from that model
    json_schema = dynamic_model.model_json_schema()
    if strict_json_schema:
        json_schema = ensure_strict_json_schema(json_schema)

    # 5. Return as a FuncSchema dataclass
    return FuncSchema(
        name=func_name,
        description=description_override or doc_info.description if doc_info else None,
        params_pydantic_model=dynamic_model,
        params_json_schema=json_schema,
        signature=sig,
        takes_context=takes_context,
        strict_json_schema=strict_json_schema,
    )
