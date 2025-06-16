"""Support for using Temporal activities as OpenAI agents tools."""

from temporalio.workflow import unsafe

with unsafe.imports_passed_through():
    from datetime import timedelta
    from typing import Any, Callable

    from agents import FunctionTool, RunContextWrapper, Tool
    from agents.function_schema import function_schema

    from temporalio import activity, workflow
    from temporalio.exceptions import ApplicationError


def activities_as_tools(*tools: Callable) -> list[Tool]:
    """Convert multiple Temporal activities to OpenAI agent tools.

    This function takes one or more Temporal activity functions and converts them
    into OpenAI agent tools that can be used by the agent to execute activities
    during workflow execution.

    Args:
        *tools: One or more Temporal activity functions to convert to tools.

    Returns:
        A list of OpenAI agent tools that wrap the provided activities.

    Example:
        @activity.defn
        def my_activity(input: str) -> str:
            return f"Processed: {input}"

        tools = activities_as_tools(my_activity)
        # Use tools with an OpenAI agent
    """
    return [activity_as_tool(tool) for tool in tools]


def activity_as_tool(fn: Callable, **kwargs) -> Tool:
    """Convert a single Temporal activity function to an OpenAI agent tool.

    This function takes a Temporal activity function and converts it into an
    OpenAI agent tool that can be used by the agent to execute the activity
    during workflow execution. The tool will automatically handle the conversion
    of inputs and outputs between the agent and the activity.

    Args:
        fn: A Temporal activity function to convert to a tool.

    Returns:
        An OpenAI agent tool that wraps the provided activity.

    Raises:
        ApplicationError: If the function is not properly decorated as a Temporal activity.

    Example:
        @activity.defn
        def process_data(input: str) -> str:
            return f"Processed: {input}"

        tool = activity_as_tool(process_data)
        # Use tool with an OpenAI agent
    """
    ret = activity._Definition.from_callable(fn)
    if not ret:
        raise ApplicationError(
            "Bare function without tool and activity decorators is not supported",
            "invalid_tool",
        )

    async def run_activity(ctx: RunContextWrapper[Any], input: str) -> Any:
        return str(
            await workflow.execute_activity(
                fn,
                input,
                **kwargs,
            )
        )

    schema = function_schema(fn)
    return FunctionTool(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=run_activity,
        strict_json_schema=True,
    )
