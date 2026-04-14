from dataclasses import replace
from typing import Any, Callable

from temporalio.contrib.langgraph.activity import wrap_activity, wrap_execute_activity
from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel
from temporalio.contrib.langgraph.task_cache import _get_task_cache, _set_task_cache, _task_id

from temporalio import activity
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Save registered graphs/entrypoints at the module level to avoid being refreshed by the sandbox.
_graph_registry: dict[str, StateGraph] = {}
_entrypoint_registry: dict[str, Pregel] = {}


class LangGraphPlugin(SimplePlugin):
    def __init__(
        self,
        # Graph API
        graphs: dict[str, StateGraph] | None = None,
        # Functional API
        entrypoints: dict[str, Pregel] | None = None,
        tasks: list | None = None,
        # TODO: Remove activity_options when we have support for @task(metadata=...)
        activity_options: dict[str, dict] | None = None,
        # TODO: Add default_activity_options that apply to all nodes or tasks
    ):
        self.activities: list = []

        # Graph API: Wrap graph nodes as Activities.
        if graphs:
            _graph_registry.update(graphs)
            for graph in graphs.values():
                for name, node in graph.nodes.items():
                    runnable = node.runnable
                    if (
                        not isinstance(runnable, RunnableCallable)
                        or runnable.afunc is None
                    ):
                        raise ValueError(f"Node {name} must have an async function")
                    # Remove LangSmith-related callback functions that can't be serialized between the workflow and activity.
                    runnable.func_accepts = {}
                    runnable.afunc = self.execute(runnable.afunc, node.metadata)

        # Functional API: Register @entrypoint functions
        if entrypoints:
            _entrypoint_registry.update(entrypoints)

        # Functional API: Wrap @task functions as Activities.
        if tasks:
            for task in tasks:
                name = task.func.__name__
                opts = (activity_options or {}).get(name, {})

                task.func = self.execute(task.func, opts)
                task.func.__name__ = name
                task.func.__qualname__ = getattr(task.func, "__qualname__", name)

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the LangGraph plugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "langchain",
                        "langchain_core",
                        "langgraph",
                        "langsmith",
                        "numpy",  # LangSmith uses numpy
                    ),
                )
            return runner

        super().__init__(
            "temporalio.LangGraphPlugin",
            activities=self.activities,
            workflow_runner=workflow_runner,
        )

    # Prepare a [node, @task] to execute as a [Activity, Workflow].
    def execute(self, func: Callable, kwargs: dict[str, Any] | None = None) -> Callable:
        execute_in = (kwargs or {}).pop("execute_in", "activity")

        if execute_in == "activity":
            a = activity.defn(name=func.__name__)(wrap_activity(func))
            self.activities.append(a)
            return wrap_execute_activity(a, task_id=_task_id(func), **(kwargs or {}))
        elif execute_in == "workflow":
            return func
        else:
            raise ValueError(f"Invalid execute_in value: {execute_in}")


def graph(name: str, cache: dict[str, Any] | None = None) -> StateGraph:
    """Retrieve a registered graph by name.

    Args:
        name: Graph name as registered with LangGraphPlugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed nodes are
            not re-executed after continue-as-new.
    """
    _patch_event_loop()
    _set_task_cache(cache or {})
    return _graph_registry[name]


def entrypoint(name: str, cache: dict[str, Any] | None = None) -> Pregel:
    """Retrieve a registered entrypoint by name.

    Args:
        name: Entrypoint name as registered with Plugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed tasks are
            not re-executed after continue-as-new.
    """
    _patch_event_loop()
    _set_task_cache(cache or {})
    return _entrypoint_registry[name]


def cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to entrypoint(name, cache=...) to
    restore cached task results across continue-as-new boundaries.
    Returns None if the cache is empty.
    """
    return _get_task_cache() or None


def _patch_event_loop():
    """Patch the event loop so LangGraph detects it as running inside Temporal's sandbox."""
    from asyncio import get_event_loop

    loop = get_event_loop()
    loop.is_running = lambda: True
