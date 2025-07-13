from langchain.tools import tool

from temporalio import activity


@tool
async def magic_function(input: int) -> int:
    """Applies a magic function to an input."""
    return input + 2


@activity.defn(name="magic_function")
async def magic_function_activity(input: int) -> int:
    """Applies a magic function to an input."""
    return input + 2
