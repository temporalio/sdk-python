"""LangGraph plugin for running LangGraph nodes and tasks as Temporal activities."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import inspect
import sys
import warnings
from dataclasses import replace
from typing import Any, Callable

from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import activity, workflow
from temporalio.contrib.langgraph._activity import wrap_activity, wrap_execute_activity
from temporalio.contrib.langgraph._interceptor import (
    LangGraphInterceptor,
    _workflow_entrypoints,
    _workflow_graphs,
)
from temporalio.contrib.langgraph._task_cache import (
    get_task_cache,
    set_task_cache,
    task_id,
)
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

_ACTIVITY_OPTION_KEYS: frozenset[str] = frozenset(
    {"execute_in", *inspect.signature(workflow.execute_activity).parameters}
)


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
        graphs: dict[str, StateGraph[Any, Any, Any, Any]] | None = None,
        # Functional API
        entrypoints: dict[str, Pregel[Any, Any, Any, Any]] | None = None,
        tasks: list | None = None,
        # TODO: Remove activity_options when we have support for @task(metadata=...)
        activity_options: dict[str, dict[str, Any]] | None = None,
        default_activity_options: dict[str, Any] | None = None,
    ):
        """Initialize the LangGraph plugin with graphs, entrypoints, and tasks."""
        if sys.version_info < (3, 11):
            warnings.warn(  # type: ignore[reportUnreachable]
                "LangGraphPlugin requires Python >= 3.11 for full async support. "
                "On older versions, the Functional API (@task/@entrypoint) and "
                "interrupt() will not work because LangGraph relies on "
                "contextvars propagation through asyncio.create_task(), which is "
                "only available in Python 3.11+. See "
                "https://reference.langchain.com/python/langgraph/config/get_store/",
                stacklevel=2,
            )

        if default_activity_options and "execute_in" in default_activity_options:
            raise ValueError(
                "execute_in cannot be set in default_activity_options. "
                "Set it on each node's metadata (Graph API) or in "
                "activity_options[task_name] (Functional API)."
            )

        self.activities: list = []

        # Graph API: Wrap graph nodes as Temporal Activities.
        if graphs:
            for graph_name, graph in graphs.items():
                for node_name, node in graph.nodes.items():
                    if node.retry_policy:
                        raise ValueError(
                            f"Node {graph_name}.{node_name} has a LangGraph "
                            f"retry_policy set. Use Temporal activity options "
                            f"instead, e.g. pass retry_policy=RetryPolicy(...) "
                            f"via default_activity_options or in the node's "
                            f"metadata dict."
                        )
                    runnable = node.runnable
                    if not isinstance(runnable, RunnableCallable):
                        raise ValueError(f"Node {node_name} must be a RunnableCallable")
                    user_func = runnable.afunc or runnable.func
                    if user_func is None:
                        raise ValueError(f"Node {node_name} must have a function")
                    # Keep 'config' (for metadata/tags) and 'runtime' (for
                    # context + store — reconstructed on the activity side).
                    # Drop writer/etc., which hold non-serializable objects
                    # that can't cross the activity boundary.
                    runnable.func_accepts = {
                        k: v
                        for k, v in runnable.func_accepts.items()
                        if k in ("config", "runtime")
                    }
                    # Split node.metadata into activity options vs. user
                    # metadata. Activity-option keys (timeouts, retry policy,
                    # etc.) become kwargs to workflow.execute_activity; user
                    # keys stay on node.metadata so LangGraph exposes them to
                    # the node function via config["metadata"].
                    node_meta = node.metadata or {}
                    node_opts = {
                        k: v for k, v in node_meta.items() if k in _ACTIVITY_OPTION_KEYS
                    }
                    node.metadata = {
                        k: v
                        for k, v in node_meta.items()
                        if k not in _ACTIVITY_OPTION_KEYS
                    }
                    if "execute_in" not in node_opts:
                        raise ValueError(
                            f"Node {graph_name}.{node_name} is missing required "
                            f"'execute_in' in metadata. Set it to 'activity' or "
                            f"'workflow'."
                        )
                    opts = {**(default_activity_options or {}), **node_opts}
                    # Route all LangGraph node calls through afunc so the async
                    # activity wrapper is always used. wrap_activity handles
                    # sync vs. async user functions inside the activity itself.
                    runnable.afunc = self.execute(
                        f"{graph_name}.{node_name}", user_func, opts
                    )
                    runnable.func = None

        # Functional API: Wrap @task functions as Temporal Activities.
        if tasks:
            for task in tasks:
                name = task.func.__name__
                if task.retry_policy:
                    raise ValueError(
                        f"Task {name} has a LangGraph retry_policy set. "
                        f"Use Temporal activity options instead, e.g. pass "
                        f"retry_policy=RetryPolicy(...) via "
                        f"default_activity_options or activity_options[{name!r}]."
                    )
                task_opts = (activity_options or {}).get(name, {})
                if "execute_in" not in task_opts:
                    raise ValueError(
                        f"Task {name} is missing required 'execute_in' in "
                        f"activity_options[{name!r}]. Set it to 'activity' or "
                        f"'workflow'."
                    )
                opts = {
                    **(default_activity_options or {}),
                    **task_opts,
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
            "langchain.LangGraphPlugin",
            activities=self.activities,
            workflow_runner=workflow_runner,
            interceptors=[LangGraphInterceptor(graphs or {}, entrypoints or {})],
        )

    def execute(
        self,
        activity_name: str,
        func: Callable,
        kwargs: dict[str, Any] | None = None,
    ) -> Callable:
        """Prepare a node or task to execute as an activity or inline in the workflow."""
        opts = kwargs or {}
        execute_in = opts.pop("execute_in")

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
) -> StateGraph[Any, Any, Any, Any]:
    """Retrieve a registered graph by name.

    Args:
        name: Graph name as registered with LangGraphPlugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed nodes are
            not re-executed after continue-as-new.
    """
    set_task_cache(cache or {})
    graphs = _workflow_graphs.get(workflow.info().run_id)
    if graphs is None:
        raise RuntimeError(
            "graph() must be called from inside a workflow running under LangGraphPlugin"
        )
    if name not in graphs:
        raise KeyError(f"Graph {name!r} not found. Available graphs: {list(graphs)}")
    return graphs[name]


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
    entrypoints = _workflow_entrypoints.get(workflow.info().run_id)
    if entrypoints is None:
        raise RuntimeError(
            "entrypoint() must be called from inside a workflow running under LangGraphPlugin"
        )
    if name not in entrypoints:
        raise KeyError(
            f"Entrypoint {name!r} not found. Available entrypoints: {list(entrypoints)}"
        )
    return entrypoints[name]


def cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to entrypoint(name, cache=...) to
    restore cached task results across continue-as-new boundaries.
    Returns None if the cache is empty.
    """
    return get_task_cache() or None
