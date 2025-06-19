"""Support for using Temporal activities as OpenAI agents tools."""

from typing import Any, Callable

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.workflow import unsafe

with unsafe.imports_passed_through():
    from agents import FunctionTool, RunContextWrapper, Tool
    from agents.function_schema import function_schema


def activity_as_tool(fn: Callable, **kwargs) -> Tool:
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
        **kwargs: Additional arguments to pass to workflow.execute_activity.
            These arguments configure how the activity is executed. Common options include:
            - start_to_close_timeout: Maximum time for the activity to complete
            - schedule_to_close_timeout: Maximum time from scheduling to completion
            - schedule_to_start_timeout: Maximum time from scheduling to starting
            - heartbeat_timeout: Maximum time between heartbeats
            - retry_policy: Policy for retrying failed activities
            - task_queue: Specific task queue to use for this activity
            - cancellation_type: How the activity handles cancellation
            - workflow_id_reuse_policy: Policy for workflow ID reuse

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
                await workflow.execute_activity(
                    fn,
                    input,
                    **kwargs,
                )
            )
        except Exception:
            raise ApplicationError(
                "You must return a string representation of the tool output, or something we can call str() on"
            )

    schema = function_schema(fn)
    return FunctionTool(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=run_activity,
        strict_json_schema=True,
    )
