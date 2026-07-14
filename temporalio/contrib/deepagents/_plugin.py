"""The plugin object users add to ``plugins=[...]``.

:class:`DeepAgentsPlugin` wires everything together so that existing
``deepagents`` code — ``create_deep_agent(...).ainvoke(...)`` — runs durably
inside a ``@workflow.defn`` with no other changes:

* registers the four activities that carry the nondeterministic work;
* installs the LangChain-aware data converter (composing, never clobbering, a
  user converter);
* passes the LangChain / LangGraph / deepagents import tree through the workflow
  sandbox;
* wraps the worker run in a ``run_context`` that patches Deep Agents' model
  resolution seam (``deepagents.graph.resolve_model``) to auto-route bare
  ``model=`` strings through activities — regardless of how the user imported
  ``create_deep_agent``;
* registers :class:`DeepAgentsWorkflowError` as a workflow-failure type;
* pushes the model / tool dispatch defaults down to the seams that read them.

The plugin auto-propagates from a ``Client`` to any ``Worker`` built from it, so
add it on exactly one side. ``configure_worker`` additionally de-duplicates
activities by name, so a user who mistakenly adds it on both sides gets a clean
no-op instead of a "More than one activity named ..." crash.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable

from temporalio import activity as activity_mod
from temporalio.contrib.deepagents import _serde, _tools
from temporalio.contrib.deepagents._activity import DeepAgentActivities
from temporalio.contrib.deepagents.workflow import DeepAgentsWorkflowError
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Runtime floor for the Deep Agents control loop. Kept in a constant so
# static checkers do not narrow `sys.version_info` comparisons into
# unreachable-code findings on new interpreters.
_MIN_PYTHON = (3, 11)


class DeepAgentsPlugin(SimplePlugin):
    """Temporal plugin that makes LangChain Deep Agents durable.

    Args:
        model_provider: Builds a real chat model from a name string, worker-side.
            This is where API keys live; only the model name ever crosses the
            workflow boundary. Defaults to LangChain's ``init_chat_model`` with
            LLM-SDK retries disabled (Temporal owns retries).
        model_activity_options: ``ActivityConfig`` (or ``Mapping[model_name,
            ActivityConfig]``) for the model activities — timeouts, retry policy.
        tool_activity_options: ``ActivityConfig`` (or ``Mapping[tool_name,
            ActivityConfig]``) default for tools wrapped with ``tool_as_activity``.
        streaming_topic: When set, model calls stream through the streaming
            activity and publish chunk batches to this workflow-streams topic.
        streaming_batch_interval: How long the streaming activity coalesces
            chunks before publishing a batch.
        passthrough_modules: Extra sandbox-passthrough modules, merged with the
            plugin's LangChain/deepagents defaults.
        data_converter: Override the default LangChain-aware converter. ``None``
            installs the default; the SDK default is upgraded in place; any other
            converter raises (fold ``DeepAgentsPayloadConverter`` into your own).
    """

    def __init__(
        self,
        *,
        model_provider: Callable[[str], Any] | None = None,
        model_activity_options: Any = None,
        tool_activity_options: Any = None,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
        passthrough_modules: Sequence[str] | None = None,
        data_converter: Any = None,
    ) -> None:
        """Configure the plugin; see the class docstring for parameters."""
        if sys.version_info < _MIN_PYTHON:
            warnings.warn(
                "DeepAgentsPlugin requires Python >= 3.11 (deepagents pins "
                ">=3.11); the Deep Agents control loop relies on contextvars "
                "propagation through asyncio that older versions lack.",
                stacklevel=2,
            )

        self._passthrough_modules = passthrough_modules
        # Held on the instance so the wiring is statically traceable: each is
        # passed straight into the call that consumes it (below), not stashed as
        # dead config.
        self._tool_activity_options = tool_activity_options
        self._data_converter = data_converter

        # Push dispatch defaults down to the model and tool seams that read them.
        # These live in ``_serde`` / ``_tools`` (langchain-free modules) so
        # constructing the plugin never imports LangChain. ``tool_activity_options``
        # is stored under ``_tools._tool_defaults`` and read back on the tool
        # dispatch path by ``_tools._resolve_tool_options(...)`` — which
        # ``tool_as_activity`` calls to compute each tool activity's timeout/retry
        # when the caller does not override ``activity_options``.
        _serde.set_settings(
            model_activity_options=model_activity_options,
            streaming_topic=streaming_topic,
        )
        # wired-via-composition: consumed on the tool dispatch path in _tools.py
        # (_resolve_tool_options), renamed to ``options`` at the callsite.
        _tools.set_tool_defaults(self._tool_activity_options)

        # The activities that carry the nondeterministic work. model_provider and
        # the batch interval are captured here so API keys never enter an input.
        self._activities = DeepAgentActivities(
            model_provider=model_provider,
            streaming_batch_interval=streaming_batch_interval,
        )
        activities = [
            self._activities.invoke_model,
            self._activities.invoke_model_streaming,
            self._activities.invoke_tool,
            self._activities.backend_op,
        ]

        super().__init__(
            "langchain.DeepAgentsPlugin",
            activities=activities,
            # wired-via-composition: ``data_converter`` flows into SimplePlugin's
            # own ``data_converter`` kwarg (renamed to ``user_converter`` inside
            # build_data_converter), which installs it on the client/worker.
            data_converter=_serde.build_data_converter(self._data_converter),
            workflow_runner=self._make_workflow_runner(),
            workflow_failure_exception_types=[DeepAgentsWorkflowError],
            run_context=self._run_context,
        )

    # -- sandbox passthrough -------------------------------------------------

    def _make_workflow_runner(
        self,
    ) -> Callable[[WorkflowRunner | None], WorkflowRunner]:
        modules = _serde.resolve_passthrough_modules(self._passthrough_modules)

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if runner is None:
                raise ValueError("No WorkflowRunner provided to DeepAgentsPlugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(*modules),
                )
            return runner

        return workflow_runner

    # -- run context ---------------------------------------------------------

    @asynccontextmanager
    async def _run_context(self) -> AsyncIterator[None]:
        """Patch Deep Agents' model resolution seam for the worker's lifetime.

        The patch (on ``deepagents.graph.resolve_model``, the seam
        ``create_deep_agent`` uses for the agent and every sub-agent) only
        rewrites ``model=`` strings when running inside a workflow, so it is inert
        on plain clients / activity workers. Determinism itself
        rides on the same mechanism ``contrib.langgraph`` uses — the loop is
        in-workflow and every nondeterministic call is an activity — so no
        speculative time/uuid shims are installed here.

        One shim *is* required: LangChain's ``create_agent`` factory wraps every
        model node with LangSmith's ``@traceable``, whose async path hops onto a
        thread via ``asyncio.run_in_executor`` — which the deterministic workflow
        event loop does not implement (it raises ``NotImplementedError``). Tracing
        is an observability concern that must not run in-workflow, so we install
        LangSmith's own Temporal escape hatch (``set_runtime_overrides``) to run
        that setup inline when ``in_workflow()``, and defer to the default thread
        hop everywhere else (activities / clients, where tracing is fine).
        """
        # Imported lazily (not at module top) so plugin construction stays
        # langchain-free; the patch is only needed once the worker is running.
        # The import itself is inside the guard: on a worker without LangChain
        # installed (e.g. one that only runs the plugin's determinism / failure
        # paths) importing ``_model`` raises ``ModuleNotFoundError``, and the
        # worker must still start — it simply runs without the auto-wrap patch.
        patched = False
        try:
            from temporalio.contrib.deepagents import _model, _tools

            _model.install_model_patch()
            _tools.install_backend_async_patch()
            patched = True
        except ImportError as exc:  # LangChain / deepagents absent on this worker
            # Deliberately ImportError only: a *missing* optional dependency
            # must not stop the worker, but a genuine patch-installation bug
            # (e.g. upstream renaming the seam raises AttributeError) must
            # surface at startup, not degrade into silent in-workflow calls.
            warnings.warn(
                f"DeepAgentsPlugin could not patch create_deep_agent ({exc}); "
                "use explicit TemporalModel(...) instances to route model calls "
                "through activities.",
                stacklevel=2,
            )
        # Installed for the life of the process, never uninstalled — see
        # _install_langsmith_temporal_override for why.
        _install_langsmith_temporal_override()
        try:
            yield
        finally:
            if patched:
                # Import is cached: patched=True implies the import above succeeded.
                from temporalio.contrib.deepagents import _model, _tools

                _model.uninstall_model_patch()
                _tools.uninstall_backend_async_patch()

    # -- worker config -------------------------------------------------------

    def configure_worker(self, config: Any) -> Any:
        """Deduplicate activity registrations after the base configuration."""
        config = super().configure_worker(config)
        activities = config.get("activities")
        if activities:
            config["activities"] = _dedupe_activities(activities)
        return config


async def _temporal_aio_to_thread(
    default_aio_to_thread: Callable[..., Any],
    ctx: Any,
    func: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run LangSmith's ``aio_to_thread`` seam safely inside a workflow.

    Outside a workflow (activities, clients) we defer to LangSmith's default
    thread hop. Inside a workflow the deterministic event loop cannot spawn a
    thread, so we run the setup inline in the requested context. ``func`` here is
    LangSmith's own run-tree bookkeeping — cheap and side-effect-free with respect
    to workflow determinism.
    """
    from temporalio import workflow

    if not workflow.in_workflow():
        return await default_aio_to_thread(ctx, func, *args, **kwargs)
    with workflow.unsafe.sandbox_unrestricted():
        return ctx.run(func, *args, **kwargs)


def _install_langsmith_temporal_override() -> None:
    """Route LangSmith's thread hop inline while in a workflow.

    ``set_runtime_overrides`` mutates a module global in the sandbox-passthrough
    ``langsmith`` package, so the in-workflow tracer sees it too. No-op (and
    harmless) when LangSmith is not installed or too old to expose
    ``set_runtime_overrides``.

    The override is deliberately installed for the life of the process and
    never uninstalled: LangSmith exposes a single process-wide override slot
    (each ``set_runtime_overrides`` call replaces it wholesale), and
    ``temporalio.contrib.langsmith`` installs a behaviorally identical override
    exactly once, without reinstalling. Resetting the slot on this worker's
    shutdown would therefore permanently strip a composed LangSmith plugin's
    workflow-safety override. Leaving it installed is safe: the override defers
    to LangSmith's default thread hop whenever ``workflow.in_workflow()`` is
    false, so it is inert outside workflows.
    """
    try:
        import langsmith

        langsmith.set_runtime_overrides(aio_to_thread=_temporal_aio_to_thread)
    except Exception:
        pass


def _dedupe_activities(activities: Sequence[Any]) -> list[Any]:
    """Drop duplicate activity registrations by defn name, keeping the first.

    Guards the "plugin added on both Client and Worker" trap, where the plugin's
    activities would otherwise be appended twice and the worker would reject the
    duplicate names.
    """
    seen: set[str] = set()
    out: list[Any] = []
    for act in activities:
        defn = activity_mod._Definition.from_callable(act)
        name = defn.name if defn is not None else getattr(act, "__name__", repr(act))
        key = name or repr(act)
        if key in seen:
            continue
        seen.add(key)
        out.append(act)
    return out
