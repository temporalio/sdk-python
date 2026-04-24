"""Activity wrappers for executing LangGraph nodes and tasks."""

from collections.abc import Awaitable
from dataclasses import dataclass
from inspect import iscoroutinefunction, signature
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt

from temporalio import workflow
from temporalio.contrib.langgraph._langgraph_config import (
    get_langgraph_config,
    set_langgraph_config,
    strip_runnable_config,
)
from temporalio.contrib.langgraph._task_cache import (
    cache_key,
    cache_lookup,
    cache_put,
)

# Per-run dedupe so we only warn once when a user passes a Store via
# graph.compile(store=...) / @entrypoint(store=...). Cleared by
# LangGraphInterceptor.execute_workflow on workflow exit.
_warned_store_runs: set[str] = set()


def clear_store_warning(run_id: str) -> None:
    """Drop the store-warning dedupe entry for a workflow run."""
    _warned_store_runs.discard(run_id)


@dataclass
class ActivityInput:
    """Input for a LangGraph activity, containing args, kwargs, and config."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    langgraph_config: dict[str, Any]


@dataclass
class ActivityOutput:
    """Output from an Activity, containing result, command, or interrupts."""

    result: Any = None
    langgraph_command: Any = None
    langgraph_interrupts: tuple[Interrupt] | None = None


def wrap_activity(
    func: Callable,
) -> Callable[[ActivityInput], Awaitable[ActivityOutput]]:
    """Wrap a function as a Temporal activity that handles LangGraph config and interrupts."""
    # Graph nodes declare `runtime: Runtime[Ctx]` in their signature; tasks
    # don't and instead reach for Runtime via get_runtime(). We re-inject the
    # reconstructed Runtime only when the user function asks.
    accepts_runtime = "runtime" in signature(func).parameters

    async def wrapper(input: ActivityInput) -> ActivityOutput:
        runtime = set_langgraph_config(input.langgraph_config)
        kwargs = dict(input.kwargs)
        if accepts_runtime:
            kwargs["runtime"] = runtime
        try:
            if iscoroutinefunction(func):
                result = await func(*input.args, **kwargs)
            else:
                result = func(*input.args, **kwargs)
            if isinstance(result, Command):
                return ActivityOutput(langgraph_command=result)
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
        # LangGraph may inject a RunnableConfig as the 'config' kwarg. Strip it
        # down to a serializable subset so it can cross the activity boundary;
        # callbacks, stores, etc. aren't serializable.
        if "config" in kwargs:
            kwargs["config"] = strip_runnable_config(kwargs["config"])

        # LangGraph may inject a Runtime as the 'runtime' kwarg. It's
        # reconstructed on the activity side from the serialized langgraph
        # config, so drop the live Runtime from the kwargs that cross the
        # activity boundary (it holds non-serializable stream_writer, store).
        runtime = kwargs.pop("runtime", None)
        run_id = workflow.info().run_id
        if (
            getattr(runtime, "store", None) is not None
            and run_id not in _warned_store_runs
        ):
            _warned_store_runs.add(run_id)
            workflow.logger.warning(
                "LangGraph Store passed via compile(store=...) / @entrypoint(store=...) "
                "is not accessible inside activity-wrapped nodes and tasks: the Store "
                "object isn't serializable across the activity boundary, and activities "
                "may run on a different worker than the workflow. Use a backend-backed "
                "store (Postgres/Redis) configured on each worker if you need shared "
                "memory, or use workflow state for per-run memory."
            )

        langgraph_config = get_langgraph_config()

        # Check task result cache (for continue-as-new deduplication).
        key = (
            cache_key(task_id, args, kwargs, langgraph_config.get("context"))
            if task_id
            else ""
        )
        if task_id:
            found, cached = cache_lookup(key)
            if found:
                return cached

        input = ActivityInput(
            args=args, kwargs=kwargs, langgraph_config=langgraph_config
        )
        output = await workflow.execute_activity(
            afunc, input, **execute_activity_kwargs
        )
        if output.langgraph_interrupts is not None:
            raise GraphInterrupt(output.langgraph_interrupts)

        result = output.result
        if output.langgraph_command is not None:
            cmd = output.langgraph_command
            result = Command(
                graph=cmd["graph"],
                update=cmd["update"],
                resume=cmd["resume"],
                goto=cmd["goto"],
            )

        # Store in cache for future continue-as-new cycles.
        if task_id:
            cache_put(key, result)

        return result

    return wrapper
