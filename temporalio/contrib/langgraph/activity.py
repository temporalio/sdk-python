from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt
from temporalio import workflow

from temporalio.contrib.langgraph.langgraph_config import get_langgraph_config, set_langgraph_config


@dataclass
class ActivityInput:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    langgraph_config: dict[str, Any]


@dataclass
class ActivityOutput:
    result: Any = None
    langgraph_interrupts: tuple[Interrupt] | None = None


def wrap_activity(func: Callable) -> Callable:
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
    afunc: Callable,
    task_id: str = "",
    **execute_activity_kwargs: dict[str, Any],
) -> Callable:
    async def wrapper(*args: Any, **kwargs: dict[str, Any]) -> Any:
        from temporalio.contrib.langgraph.task_cache import _cache_key, _cache_lookup, _cache_put

        # Check task result cache (for continue-as-new deduplication).
        key = _cache_key(task_id, args, kwargs) if task_id else ""
        if task_id:
            found, cached = _cache_lookup(key)
            if found:
                return cached

        input = ActivityInput(
            args=args, kwargs=kwargs, langgraph_config=get_langgraph_config()
        )
        output: ActivityOutput = await workflow.execute_activity(
            afunc, input, result_type=ActivityOutput, **execute_activity_kwargs
        )
        if output.langgraph_interrupts is not None:
            raise GraphInterrupt(output.langgraph_interrupts)

        # Store in cache for future continue-as-new cycles.
        if task_id:
            _cache_put(key, output.result)

        return output.result

    return wrapper
