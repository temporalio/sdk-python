"""LangGraph plugin for running LangGraph nodes and tasks as Temporal activities."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import inspect
import sys
import warnings
from dataclasses import replace
from typing import Any

from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph

from temporalio import activity, workflow
from temporalio.contrib.langgraph.activity import wrap_activity, wrap_execute_activity
from temporalio.contrib.langgraph.task_cache import _task_cache, task_id
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

    Pass your graphs and tasks to the plugin; the plugin mutates them in place so
    node/task invocations dispatch to Temporal activities. The modules those
    graphs and tasks are defined in are automatically added to the workflow
    sandbox's passthrough list, so the mutation is visible inside the sandbox.
    Keep your ``@workflow.defn`` classes in a module separate from your graphs
    and tasks (the standard Temporal convention).
    """

    def __init__(
        self,
        graphs: list[StateGraph[Any, Any, Any, Any]] | None = None,
        tasks: list | None = None,
        activity_options: dict[str, dict] | None = None,
        default_activity_options: dict[str, Any] | None = None,
    ):
        """Register activities for graphs and tasks."""
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

        self.activities: list = []
        passthrough_modules: set[str] = set()

        if graphs:
            for graph in graphs:
                for node_name, node in graph.nodes.items():
                    runnable = node.runnable
                    if (
                        not isinstance(runnable, RunnableCallable)
                        or runnable.afunc is None
                    ):
                        raise ValueError(
                            f"Node {node_name} must have an async function"
                        )
                    # Keep only 'config' injection so node functions can read
                    # metadata/tags. Drop writer/store/runtime/etc., which hold
                    # non-serializable objects that can't cross the activity
                    # boundary. The wrapper serializes config down to its
                    # portable subset before handing off to the activity.
                    runnable.func_accepts = {
                        k: v for k, v in runnable.func_accepts.items() if k == "config"
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
                    opts = {**(default_activity_options or {}), **node_opts}
                    runnable.afunc = self._wrap(
                        runnable.afunc, opts, passthrough_modules
                    )

        if tasks:
            for t in tasks:
                name = t.func.__name__
                qualname = getattr(t.func, "__qualname__", name)
                opts = {
                    **(default_activity_options or {}),
                    **(activity_options or {}).get(name, {}),
                }
                t.func = self._wrap(t.func, opts, passthrough_modules)
                t.func.__name__ = name
                t.func.__qualname__ = qualname

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
                        *passthrough_modules,
                    ),
                )
            return runner

        super().__init__(
            "temporalio.LangGraphPlugin",
            activities=self.activities,
            workflow_runner=workflow_runner,
        )

    def _wrap(
        self,
        func: Any,
        opts: dict[str, Any],
        passthrough_modules: set[str],
    ) -> Any:
        """Wrap a node afunc or task func as an activity. Idempotent across plugins.

        Records the activity defn on ``self.activities`` and the function's
        origin module on ``passthrough_modules``. If ``func`` is already wrapped
        (e.g., a second plugin sharing the same graph), reuses the cached
        activity defn and module — no double-wrap.
        """
        meta = getattr(func, "_temporal_meta", None)
        if meta is not None:
            a, module = meta
            if a is not None:
                self.activities.append(a)
            if module:
                passthrough_modules.add(module)
            return func

        module = getattr(func, "__module__", None)
        execute_in = opts.pop("execute_in", "activity")
        if execute_in == "activity":
            activity_name = task_id(func)
            a = activity.defn(name=activity_name)(wrap_activity(func))
            self.activities.append(a)
            wrapped = wrap_execute_activity(a, task_id=activity_name, **opts)
        elif execute_in == "workflow":
            a = None
            wrapped = func
        else:
            raise ValueError(f"Invalid execute_in value: {execute_in}")

        if module:
            passthrough_modules.add(module)
        try:
            setattr(wrapped, "_temporal_meta", (a, module))
        except (AttributeError, TypeError):
            pass
        return wrapped


def set_cache(cache: dict[str, Any] | None) -> None:
    """Restore a task result cache returned by a previous :func:`get_cache` call.

    Use at the top of a workflow run that resumes from continue-as-new so
    already-completed nodes/tasks are not re-executed.
    """
    _task_cache.set(cache or {})


def get_cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to :func:`set_cache` on the next
    workflow run to restore cached task results across continue-as-new
    boundaries. Returns None if the cache is empty.
    """
    return _task_cache.get() or None
