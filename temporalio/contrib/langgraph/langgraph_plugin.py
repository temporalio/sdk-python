"""LangGraph plugin for running LangGraph nodes and tasks as Temporal activities."""

# pyright: reportMissingTypeStubs=false

from dataclasses import replace
from typing import Any, Callable

from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import activity
from temporalio.contrib.langgraph.activity import wrap_activity, wrap_execute_activity
from temporalio.contrib.langgraph.langgraph_interceptor import LangGraphInterceptor
from temporalio.contrib.langgraph.task_cache import (
    get_task_cache,
    set_task_cache,
    task_id,
)
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Save registered graphs/entrypoints at the module level to avoid being refreshed by the sandbox.
_graph_registry: dict[str, StateGraph[Any]] = {}
_entrypoint_registry: dict[str, Pregel[Any, Any, Any, Any]] = {}


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

        # Graph API: Wrap graph nodes as Temporal Activities.
        if graphs:
            _graph_registry.update(graphs)
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

        if entrypoints:
            _entrypoint_registry.update(entrypoints)

        # Functional API: Wrap @task functions as Temporal Activities.
        if tasks:
            for task in tasks:
                name = task.func.__name__
                opts = {
                    **(default_activity_options or {}),
                    **(activity_options or {}).get(name, {}),
                }

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
            interceptors=[LangGraphInterceptor()],
        )

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
    set_task_cache(cache or {})
    if name not in _graph_registry:
        raise KeyError(
            f"Graph {name!r} not found. "
            f"Available graphs: {list(_graph_registry.keys())}"
        )
    return _graph_registry[name]


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
    set_task_cache(cache or {})
    if name not in _entrypoint_registry:
        raise KeyError(
            f"Entrypoint {name!r} not found. "
            f"Available entrypoints: {list(_entrypoint_registry.keys())}"
        )
    return _entrypoint_registry[name]


def cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to entrypoint(name, cache=...) to
    restore cached task results across continue-as-new boundaries.
    Returns None if the cache is empty.
    """
    return get_task_cache() or None
