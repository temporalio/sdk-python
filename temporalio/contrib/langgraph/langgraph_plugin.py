"""LangGraph plugin for running LangGraph nodes and tasks as Temporal activities."""

# pyright: reportMissingTypeStubs=false

from dataclasses import replace
from typing import Any, Callable

from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import activity, workflow
from temporalio.contrib.langgraph.activity import wrap_activity, wrap_execute_activity
from temporalio.contrib.langgraph.task_cache import (
    get_task_cache,
    set_task_cache,
    task_id,
)
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkerConfig, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Save registered graphs/entrypoints at the module level to avoid being refreshed by the sandbox.
# Keyed by task queue to isolate concurrent Workers/Plugins in the same process.
_graph_registry: dict[str, dict[str, StateGraph[Any]]] = {}
_entrypoint_registry: dict[str, dict[str, Pregel[Any, Any, Any, Any]]] = {}


class LangGraphPlugin(SimplePlugin):
    """LangGraph plugin for Temporal SDK.

    .. warning::
        This package is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin runs `LangGraph <https://github.com/langchain-ai/langgraph>`_ nodes
    and tasks as Temporal Activities, giving your AI agent workflows durable
    execution, automatic retries, and timeouts. It supports both the LangGraph Graph
    API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).
    """

    def __init__(
        self,
        # Graph API
        graphs: dict[str, StateGraph] | None = None,
        # Functional API
        entrypoints: dict[str, Pregel[Any, Any, Any, Any]] | None = None,
        tasks: list | None = None,
        # TODO: Remove activity_options when we have support for @task(metadata=...)
        activity_options: dict[str, dict] | None = None,
        default_activity_options: dict[str, Any] | None = None,
    ):
        """Initialize the LangGraph plugin with graphs, entrypoints, and tasks."""
        self.activities: list = []
        self._graphs: dict[str, StateGraph[Any]] = graphs or {}
        self._entrypoints: dict[str, Pregel[Any, Any, Any, Any]] = entrypoints or {}

        # Graph API: Wrap graph nodes as Temporal Activities.
        if graphs:
            for graph_name, graph in graphs.items():
                for node_name, node in graph.nodes.items():
                    runnable = node.runnable
                    if (
                        not isinstance(runnable, RunnableCallable)
                        or runnable.afunc is None
                    ):
                        raise ValueError(
                            f"Node {node_name} must have an async function"
                        )
                    # Remove LangSmith-related callback functions that can't be serialized between the workflow and activity.
                    runnable.func_accepts = {}
                    opts = {**(default_activity_options or {}), **(node.metadata or {})}
                    runnable.afunc = self.execute(
                        f"{graph_name}.{node_name}", runnable.afunc, opts
                    )

        # Functional API: Wrap @task functions as Temporal Activities.
        if tasks:
            for task in tasks:
                name = task.func.__name__
                opts = {**(default_activity_options or {}), **(activity_options or {}).get(name, {})}

                task.func = self.execute(task_id(task.func), task.func, opts)
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

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """Register graphs/entrypoints scoped to the worker's task queue."""
        task_queue = config.get("task_queue")
        if not task_queue:
            raise ValueError(
                "Worker config must include a task_queue for LangGraphPlugin"
            )
        if self._graphs:
            _graph_registry.setdefault(task_queue, {}).update(self._graphs)
        if self._entrypoints:
            _entrypoint_registry.setdefault(task_queue, {}).update(self._entrypoints)
        return super().configure_worker(config)

    def execute(
        self,
        activity_name: str,
        func: Callable,
        kwargs: dict[str, Any] | None = None,
    ) -> Callable:
        """Prepare a node or task to execute as an activity or inline in the workflow."""
        opts = kwargs or {}
        execute_in = opts.pop("execute_in", "activity")

        if execute_in == "activity":
            a = activity.defn(name=activity_name)(wrap_activity(func))
            self.activities.append(a)
            return wrap_execute_activity(a, task_id=task_id(func), **opts)
        elif execute_in == "workflow":
            return func
        else:
            raise ValueError(f"Invalid execute_in value: {execute_in}")


def graph(
    name: str, cache: dict[str, Any] | None = None
) -> StateGraph[Any, None, Any, Any]:
    """Retrieve a registered graph by name.

    Args:
        name: Graph name as registered with LangGraphPlugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed nodes are
            not re-executed after continue-as-new.
    """
    _patch_event_loop()
    set_task_cache(cache or {})
    task_queue = workflow.info().task_queue
    registry = _graph_registry.get(task_queue, {})
    if name not in registry:
        raise KeyError(
            f"Graph {name!r} not found for task queue {task_queue!r}. "
            f"Available graphs: {list(registry.keys())}"
        )
    return registry[name]


def entrypoint(
    name: str, cache: dict[str, Any] | None = None
) -> Pregel[Any, Any, Any, Any]:
    """Retrieve a registered entrypoint by name.

    Args:
        name: Entrypoint name as registered with Plugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed tasks are
            not re-executed after continue-as-new.
    """
    _patch_event_loop()
    set_task_cache(cache or {})
    task_queue = workflow.info().task_queue
    registry = _entrypoint_registry.get(task_queue, {})
    if name not in registry:
        raise KeyError(
            f"Entrypoint {name!r} not found for task queue {task_queue!r}. "
            f"Available entrypoints: {list(registry.keys())}"
        )
    return registry[name]


def cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to entrypoint(name, cache=...) to
    restore cached task results across continue-as-new boundaries.
    Returns None if the cache is empty.
    """
    return get_task_cache() or None


def _patch_event_loop():
    """Patch the event loop so LangGraph detects it as running inside Temporal's sandbox."""
    from asyncio import get_event_loop

    loop = get_event_loop()
    setattr(loop, "is_running", lambda: True)
