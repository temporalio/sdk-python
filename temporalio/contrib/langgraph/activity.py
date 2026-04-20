"""Activity wrappers for executing LangGraph nodes and tasks."""

from collections.abc import Awaitable
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from temporalio import workflow
from temporalio.contrib.langgraph.langgraph_config import (
    get_langgraph_config,
    set_langgraph_config,
)
from temporalio.contrib.langgraph.task_cache import (
    cache_key,
    cache_lookup,
    cache_put,
)


@dataclass
class ActivityInput:
    """Input for a LangGraph activity, containing args, kwargs, and config."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    langgraph_config: dict[str, Any]


@dataclass
class ActivityOutput:
    """Output from a LangGraph activity, containing result or interrupts."""

    result: Any = None
    langgraph_interrupts: tuple[Interrupt] | None = None


def wrap_activity(
    func: Callable,
) -> Callable[[ActivityInput], Awaitable[ActivityOutput]]:
    """Wrap a function as a Temporal activity that handles LangGraph config and interrupts."""

    async def wrapper(input: ActivityInput) -> ActivityOutput:
        set_langgraph_config(input.langgraph_config)
        try:
            if iscoroutinefunction(func):
                result = await func(*input.args, **input.kwargs)
            else:
                result = func(*input.args, **input.kwargs)
            return ActivityOutput(result=result)
        except GraphInterrupt as e:
            return ActivityOutput(langgraph_interrupts=e.args[0])

    return wrapper


def wrap_execute_activity(
    afunc: Callable[[ActivityInput], Awaitable[ActivityOutput]],
    task_id: str = "",
    **execute_activity_kwargs: Any,
) -> Callable[..., Any]:
    """Wrap an activity function to be called via workflow.execute_activity with caching."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Check task result cache (for continue-as-new deduplication).
        key = cache_key(task_id, args, kwargs) if task_id else ""
        if task_id:
            found, cached = cache_lookup(key)
            if found:
                return cached

        input = ActivityInput(
            args=args, kwargs=kwargs, langgraph_config=get_langgraph_config()
        )
        output = await workflow.execute_activity(
            afunc, input, **execute_activity_kwargs
        )
        if output.langgraph_interrupts is not None:
            raise GraphInterrupt(output.langgraph_interrupts)

        # Store in cache for future continue-as-new cycles.
        if task_id:
            cache_put(key, output.result)

        return output.result

    return wrapper
