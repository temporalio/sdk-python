"""Temporal-specific utilities for LangChain integration."""

from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from pydantic import create_model
from typing_extensions import get_type_hints

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.langchain._model_activity import (
    ActivityModelInput,
    ModelActivity,
    ModelOutput,
)
from temporalio.contrib.langchain._model_parameters import ModelActivityParameters
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from langchain_core.tools import BaseTool
from pydantic import Field


class ToolSerializationError(Exception):
    """Error that occurs when a tool output could not be serialized."""


class TemporalActivityTool(BaseTool):
    """A LangChain BaseTool that wraps a Temporal activity."""

    name: str = Field(...)
    description: str = Field(...)
    args_schema: Any = Field(default=None)

    def __init__(
        self,
        name: str,
        description: str,
        args_schema: Any,
        execute_func: Callable,
        **kwargs,
    ):
        super().__init__(
            name=name, description=description, args_schema=args_schema, **kwargs
        )
        self._execute_func = execute_func

    def _run(self, **kwargs) -> str:
        """Synchronous run method - not recommended in workflows."""
        raise NotImplementedError("Use async methods (ainvoke/invoke) in workflows")

    async def _arun(self, **kwargs) -> Any:
        """Async run method that executes the Temporal activity."""
        return await self._execute_func(**kwargs)

    async def invoke(self, **kwargs) -> Any:
        """Override invoke to handle keyword arguments directly."""
        return await self._execute_func(**kwargs)

    async def ainvoke(self, input_data, config=None, **kwargs) -> Any:
        """LangChain async invoke method."""
        if isinstance(input_data, dict):
            return await self._execute_func(**input_data)
        else:
            return await self._execute_func(input_data, **kwargs)

    def __getitem__(self, key):
        """Support dictionary-style access for backward compatibility."""
        if key == "name":
            return self.name
        elif key == "description":
            return self.description
        elif key == "args_schema":
            return self.args_schema
        elif key == "execute":
            return self._execute_func
        else:
            raise KeyError(f"'{key}' not found in TemporalActivityTool")

    def __contains__(self, key):
        """Support 'in' operator for backward compatibility."""
        return key in ["name", "description", "args_schema", "execute"]


class workflow:
    """Encapsulates workflow specific primitives for working with LangChain in a workflow context."""

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
    ) -> Dict[str, Any]:
        """Convert a Temporal activity function to a LangChain tool specification.

        This function takes a Temporal activity function and converts it into a
        tool specification that can be used with LangChain models. The tool will
        automatically handle the execution of the activity during workflow execution.

        Args:
            fn: A Temporal activity function to convert to a tool.
            For other arguments, refer to :py:mod:`workflow` :py:meth:`start_activity`

        Returns:
            A dictionary containing the tool specification for LangChain.

        Raises:
            ApplicationError: If the function is not properly decorated as a Temporal activity.

        Example:
            >>> @activity.defn
            >>> def process_data(input: str) -> str:
            ...     return f"Processed: {input}"
            >>>
            >>> # Create tool specification
            >>> tool_spec = workflow.activity_as_tool(
            ...     process_data,
            ...     start_to_close_timeout=timedelta(seconds=30),
            ...     retry_policy=RetryPolicy(maximum_attempts=3)
            ... )
        """
        # Check if function is a Temporal activity
        activity_defn = activity._Definition.from_callable(fn)
        if not activity_defn:
            raise ApplicationError(
                "Function must be decorated with @activity.defn",
                "invalid_activity",
            )

        # Get function signature and type hints
        type_hints = get_type_hints(fn)
        func_name = fn.__name__
        func_doc = fn.__doc__ or f"Execute {func_name} activity"

        # Create Pydantic model from function signature
        import inspect

        sig = inspect.signature(fn)

        # Build fields for the Pydantic model
        fields = {}
        for param_name, param in sig.parameters.items():
            param_type = type_hints.get(param_name, Any)
            default = param.default if param.default != inspect.Parameter.empty else ...
            fields[param_name] = (param_type, default)

        # Create the args schema
        if fields:
            args_schema = create_model(f"{func_name}Args", **fields)
        else:
            args_schema = None

        async def execute_tool(*args, **kwargs) -> str:
            """Execute the activity with the given arguments."""
            # Handle both positional and keyword arguments
            if args and not kwargs:
                # Only positional arguments provided
                activity_args = args
            elif kwargs and not args:
                # Only keyword arguments provided - convert to positional args in the correct order
                activity_args = []
                for param_name in sig.parameters.keys():
                    if param_name in kwargs:
                        activity_args.append(kwargs[param_name])
            elif args and kwargs:
                # Both positional and keyword arguments provided
                # This typically happens when ainvoke passes input_data as positional and other params as kwargs
                activity_args = list(args)
                param_names = list(sig.parameters.keys())
                for param_name in param_names[len(args) :]:
                    if param_name in kwargs:
                        activity_args.append(kwargs[param_name])
            else:
                # No arguments
                activity_args = []

            # Execute the activity with the correct signature based on parameter count
            if len(sig.parameters) == 0:
                # No-parameter activity
                result = await temporal_workflow.execute_activity(
                    fn,
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
            elif len(sig.parameters) == 1:
                # Single-parameter activity - pass the argument directly (not as a list)
                if isinstance(activity_args, (list, tuple)) and len(activity_args) == 1:
                    result = await temporal_workflow.execute_activity(
                        fn,
                        activity_args[0],
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
                else:
                    # Direct argument (not in a list)
                    result = await temporal_workflow.execute_activity(
                        fn,
                        activity_args,
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
            else:
                # Multi-parameter activity - use args parameter
                result = await temporal_workflow.execute_activity(
                    fn,
                    args=activity_args
                    if isinstance(activity_args, (list, tuple))
                    else [activity_args],
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

            # Return result as-is to preserve type information
            # LangChain tools can return various types, not just strings
            return result

        return TemporalActivityTool(
            name=func_name,
            description=func_doc,
            args_schema=args_schema,
            execute_func=execute_tool,
        )

    @classmethod
    async def invoke_model(
        cls,
        model_activity: ModelActivity,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        activity_params: Optional[ModelActivityParameters] = None,
    ) -> ModelOutput:
        """Invoke a LangChain model as a Temporal activity.

        Args:
            model_activity: The ModelActivity instance to use
            messages: List of message dictionaries
            tools: Optional list of tool specifications
            temperature: Optional temperature for the model
            max_tokens: Optional max tokens for the model
            model_kwargs: Additional model parameters
            activity_params: Activity execution parameters

        Returns:
            ModelOutput containing the model's response
        """
        if activity_params is None:
            activity_params = ModelActivityParameters()

        # Convert tool specifications to LangChainToolInput format
        tool_inputs = []
        if tools:
            for tool in tools:
                tool_input = {
                    "name": tool["name"],
                    "description": tool["description"],
                    "args_schema": tool.get("args_schema"),
                }
                tool_inputs.append(tool_input)

        # Prepare the activity input
        activity_input = ActivityModelInput(
            messages=messages,
            tools=tool_inputs,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs=model_kwargs or {},
        )

        # Execute the model activity
        return await temporal_workflow.execute_activity(
            model_activity.invoke_model_activity,
            activity_input,
            task_queue=activity_params.task_queue,
            schedule_to_close_timeout=activity_params.schedule_to_close_timeout,
            schedule_to_start_timeout=activity_params.schedule_to_start_timeout,
            start_to_close_timeout=activity_params.start_to_close_timeout,
            heartbeat_timeout=activity_params.heartbeat_timeout,
            retry_policy=activity_params.retry_policy,
            cancellation_type=activity_params.cancellation_type,
            versioning_intent=activity_params.versioning_intent,
            summary=activity_params.summary_override,
            priority=activity_params.priority,
        )

    @classmethod
    async def invoke_model_with_tools(
        cls,
        model_activity: ModelActivity,
        messages: List[Dict[str, Any]],
        available_tools: List[Dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        activity_params: Optional[ModelActivityParameters] = None,
        max_iterations: int = 10,
    ) -> ModelOutput:
        """Invoke a model with tools and handle tool execution automatically.

        This method will automatically execute tools when the model requests them,
        creating a conversation loop until the model provides a final answer.

        Args:
            model_activity: The ModelActivity instance to use
            messages: List of message dictionaries
            available_tools: List of available tool specifications
            temperature: Optional temperature for the model
            max_tokens: Optional max tokens for the model
            model_kwargs: Additional model parameters
            activity_params: Activity execution parameters
            max_iterations: Maximum number of model/tool iterations

        Returns:
            ModelOutput containing the final model response
        """
        current_messages = messages.copy()

        for iteration in range(max_iterations):
            # Invoke the model
            response = await cls.invoke_model(
                model_activity,
                current_messages,
                tools=available_tools,
                temperature=temperature,
                max_tokens=max_tokens,
                model_kwargs=model_kwargs,
                activity_params=activity_params,
            )

            # If no tool calls, we're done
            if not response.tool_calls:
                return response

            # Add the assistant's response to messages
            current_messages.append(
                {
                    "type": "ai",
                    "content": response.content,
                    "tool_calls": response.tool_calls,
                }
            )

            # Execute each tool call
            for i, tool_call in enumerate(response.tool_calls):
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call.get("id", f"tool_call_{i}")

                # Find the tool
                tool = None
                for available_tool in available_tools:
                    if available_tool["name"] == tool_name:
                        tool = available_tool
                        break

                if tool is None:
                    tool_result = f"Error: Tool {tool_name} not found"
                else:
                    try:
                        # Execute the tool
                        tool_result = await tool["execute"](**tool_args)
                    except Exception as e:
                        tool_result = f"Error executing tool {tool_name}: {str(e)}"

                # Add tool result to messages
                current_messages.append(
                    {
                        "type": "tool",
                        "content": tool_result,
                        "tool_call_id": tool_id,
                    }
                )

        # If we've exceeded max iterations, return the last response
        return response
