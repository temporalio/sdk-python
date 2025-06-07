from temporalio.workflow import unsafe

with unsafe.imports_passed_through():
    from datetime import timedelta
    from typing import Any, Callable

    from agents import FunctionTool, RunContextWrapper, Tool
    from agents.function_schema import function_schema
    from temporalio import activity, workflow
    from temporalio.exceptions import ApplicationError


def activities_as_tools(*tools: Callable) -> list[Tool]:
    """Convert activities to tools."""
    return [activity_as_tool(tool) for tool in tools]


def activity_as_tool(fn: Callable) -> Tool:
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
                start_to_close_timeout=timedelta(seconds=10),
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
