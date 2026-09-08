"""The tool + backend seams: the explicit per-unit Workflow-vs-Activity choice.

Deep Agents holds its tools and filesystem/shell backends in-workflow. A tool or
backend op that only reads and writes ``DeepAgentState`` is pure and belongs in
the workflow (deterministic, replay-safe). One that does real I/O — a web
search, a shell command, a disk write — must not run there. This module gives
the user three explicit ways to move that work to an activity:

* :func:`activity_as_tool` — expose an existing ``@activity.defn`` as a Deep
  Agents tool (Temporal adopters already have activities; don't make them
  re-declare);
* :func:`tool_as_activity` — wrap a LangChain ``BaseTool`` / callable so its
  execution runs as an activity;
* :class:`TemporalBackend` — wrap a real-I/O backend so each file/exec op runs
  as an activity.

The choice is always explicit: an unwrapped non-builtin tool runs in-workflow,
and the plugin warns at construction so that is a conscious decision, never a
silent one.

Registries here live in a ``temporalio``-namespaced (sandbox-passthrough) module,
so the object the worker's activity sees is the same one the module-level
``tool_as_activity(...)`` / ``TemporalBackend(...)`` call populated. They hold
worker-wide wiring, not per-workflow state.
"""

from __future__ import annotations

import importlib
import threading
import uuid as _uuid
import warnings
import weakref
from collections.abc import Mapping
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable

from temporalio import activity as activity_mod
from temporalio import workflow
from temporalio.contrib.deepagents import _activity, _serde

# LangChain is a runtime dependency of the *tool seam*, but importing this module
# must not require it (the plugin imports it just to read tool defaults). So the
# ``langchain_core.tools`` symbols are imported lazily inside the functions that
# actually build tools; ``from __future__ import annotations`` keeps the type
# hints below as strings so they never touch LangChain at import time.
if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


# ---------------------------------------------------------------------------
# Tool registry (worker-side execution targets)
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, "BaseTool"] = {}
_BACKEND_REGISTRY: dict[str, Any] = {}
# Serializes registration against the GC-time unregister in
# _unregister_backend, which may run on another thread.
_BACKEND_REGISTRY_LOCK = threading.Lock()

# Worker-wide default activity options for tools wrapped with tool_as_activity,
# set by the plugin. Not per-workflow state: fixed for the worker's lifetime, and
# this module is sandbox-passthrough so the workflow sees the configured value.
_tool_defaults: dict[str, Any] = {}


def set_tool_defaults(options: Any) -> None:
    """Install the plugin's default ``tool_activity_options`` (called by the plugin).

    Accepts a single ``ActivityConfig`` or a ``Mapping[tool_name, ActivityConfig]``.
    """
    _tool_defaults.clear()
    if options:
        _tool_defaults["__value__"] = options


def _resolve_tool_options(
    tool_name: str, instance_options: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge plugin tool defaults (possibly per-tool) with a per-call override."""
    opts: dict[str, Any] = {}
    default = _tool_defaults.get("__value__")
    if isinstance(default, Mapping):
        per_tool = default.get(tool_name)
        if isinstance(per_tool, Mapping):
            opts.update(per_tool)
        elif not any(isinstance(v, Mapping) for v in default.values()):
            opts.update(default)
    if instance_options:
        opts.update(instance_options)
    return opts


# Names of tools that route through Temporal (activity_as_tool / tool_as_activity),
# used to warn about unwrapped non-builtin tools at workflow build time.
_ROUTED_TOOL_NAMES: set[str] = set()

# Deep Agents' built-in tools run in-workflow (pure state mutations); they are
# not expected to be wrapped, so they never trigger the unwrapped-tool warning.
# These are the LLM-facing TOOL names (``read_file``, ``write_file``, …) —
# NOT the backend protocol method names in ``_BACKEND_OPS`` (``read``,
# ``aread``, …); the two namespaces intentionally differ.
_BUILTIN_TOOL_NAMES = frozenset(
    {
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "task",
    }
)


def register_tool(tool: BaseTool) -> None:
    """Record ``tool`` so :meth:`DeepAgentActivities.invoke_tool` can run it."""
    _TOOL_REGISTRY[tool.name] = tool


def warn_unwrapped_tools(tools: Any) -> None:
    """Warn once per unwrapped, non-builtin tool passed to ``create_deep_agent``.

    Running a tool in-workflow is only safe if it is pure/deterministic. The
    Workflow-vs-Activity choice must be conscious, so any user tool that was not
    routed through :func:`tool_as_activity` / :func:`activity_as_tool` gets a
    construction-time warning rather than silently executing in the workflow.
    """
    for tool in tools or ():
        name = getattr(tool, "name", getattr(tool, "__name__", None))
        if name is None or name in _BUILTIN_TOOL_NAMES or name in _ROUTED_TOOL_NAMES:
            continue
        warnings.warn(
            f"Tool {name!r} is passed to create_deep_agent unwrapped and will run "
            f"inside the workflow. That is only safe if it is pure/deterministic. "
            f"If it does I/O, wrap it with tool_as_activity(...) or expose an "
            f"existing activity with activity_as_tool(...).",
            stacklevel=3,
        )


def get_registered_tool(name: str) -> BaseTool | None:
    """Look up a tool registered by :func:`register_tool`."""
    return _TOOL_REGISTRY.get(name)


def register_backend(ref: str, backend: Any) -> None:
    """Record a backend so :meth:`DeepAgentActivities.backend_op` can reach it."""
    with _BACKEND_REGISTRY_LOCK:
        _BACKEND_REGISTRY[ref] = backend


def _unregister_backend(ref: str, inner: Any) -> None:
    """Drop ``ref`` from the registry if it still maps to ``inner``.

    GC hook for :class:`TemporalBackend` (via ``weakref.finalize``): a wrapper
    is typically constructed per workflow run, so without cleanup a long-lived
    worker accumulates one registry entry per run. The identity guard is
    load-bearing: refs are deterministic per run, so after a cache eviction a
    replay re-registers the *same* ref with a fresh inner backend — the evicted
    wrapper's finalizer must not remove that live registration.
    """
    with _BACKEND_REGISTRY_LOCK:
        if _BACKEND_REGISTRY.get(ref) is inner:
            del _BACKEND_REGISTRY[ref]


def registered_backends() -> dict[str, Any]:
    """Return the live backend registry (read by the plugin at worker build)."""
    return _BACKEND_REGISTRY


# ---------------------------------------------------------------------------
# activity_as_tool
# ---------------------------------------------------------------------------


def activity_as_tool(
    activity: Callable,
    *,
    start_to_close_timeout: timedelta,
    name: str | None = None,
    description: str | None = None,
    retry_policy: Any = None,
    summary: str | None = None,
) -> BaseTool:
    """Expose an existing Temporal activity as a Deep Agents tool.

    The returned tool advertises the activity's argument schema to the model and,
    when called in-workflow, dispatches to the activity via
    ``workflow.execute_activity`` — Temporal owns its retries and timeout.

    Args:
        activity: A function decorated with ``@activity.defn``.
        start_to_close_timeout: Required per-call timeout for the activity.
        name: Override the tool name advertised to the model (defaults to
            the activity definition name).
        description: Override the tool description advertised to the model
            (defaults to the activity docstring).
        retry_policy: Optional Temporal retry policy for the activity.
        summary: Optional ``summary=`` recorded on each activity invocation.
    """
    with workflow.unsafe.imports_passed_through():
        from langchain_core.tools import StructuredTool

    defn = activity_mod._Definition.from_callable(activity)
    if defn is None:
        raise ValueError(
            "activity_as_tool requires a function decorated with @activity.defn; "
            f"{getattr(activity, '__name__', activity)!r} is not an activity."
        )
    tool_name = name or defn.name
    if tool_name is None:
        raise ValueError(
            "activity_as_tool requires a named activity (dynamic activities "
            "have no definition name); pass name= explicitly."
        )
    tool_desc = description or (activity.__doc__ or f"Temporal activity {tool_name}.")
    act_summary = summary or f"tool:{tool_name}"

    # Temporal activities take a single positional argument. StructuredTool infers
    # the model-facing schema from ``_run``'s signature, so we mirror the activity's
    # own parameter names onto ``_run`` (via ``@wraps``) and then collapse the
    # keyword call the model produces back into that single positional payload.
    import inspect

    params = [
        p for p in inspect.signature(activity).parameters if p not in ("self", "cls")
    ]

    @wraps(activity)
    async def _run(*args: Any, **kwargs: Any) -> Any:
        if args and not kwargs:
            payload: Any = args[0] if len(args) == 1 else list(args)
        elif len(params) == 1 and len(kwargs) == 1:
            # Single-argument activity: pass the value directly, not {"arg": value}.
            payload = next(iter(kwargs.values()))
        else:
            payload = kwargs
        return await workflow.execute_activity(
            activity,
            payload,
            start_to_close_timeout=start_to_close_timeout,
            retry_policy=retry_policy,
            summary=act_summary,
        )

    _ROUTED_TOOL_NAMES.add(tool_name)
    return StructuredTool.from_function(
        coroutine=_run,
        name=tool_name,
        description=tool_desc,
    )


# ---------------------------------------------------------------------------
# tool_as_activity
# ---------------------------------------------------------------------------


def tool_as_activity(
    tool: BaseTool | Callable,
    *,
    start_to_close_timeout: timedelta,
    activity_options: Mapping[str, Any] | None = None,
) -> BaseTool:
    """Wrap a LangChain tool / callable so its execution runs as an activity.

    The underlying tool is registered on the worker; the returned tool keeps the
    same name and argument schema (so the model's calls are unchanged) but, when
    invoked in-workflow, dispatches ``deepagents.invoke_tool`` instead of running
    the tool body inline.
    """
    with workflow.unsafe.imports_passed_through():
        from langchain_core.tools import BaseTool, StructuredTool

    base_tool: BaseTool
    if isinstance(tool, BaseTool):
        base_tool = tool
    else:
        base_tool = StructuredTool.from_function(
            tool if not _is_coroutine(tool) else None,
            coroutine=tool if _is_coroutine(tool) else None,
        )
    register_tool(base_tool)

    tool_name = base_tool.name
    opts = _resolve_tool_options(tool_name, activity_options)
    opts.setdefault("start_to_close_timeout", start_to_close_timeout)

    async def _run(**kwargs: Any) -> Any:
        from temporalio.contrib.deepagents.workflow import call_tool

        tool_call_id = kwargs.pop("__tool_call_id__", None) or workflow.uuid4().hex
        activity_input = _activity.ToolActivityInput(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=kwargs,
        )
        output = await call_tool(
            activity_input,
            summary=f"tool:{tool_name}",
            **opts,
        )
        message = _serde.load_object(output.message)
        # Return CONTENT, not the pre-built ToolMessage: the activity cannot
        # know the model's real tool_call_id, so a ToolMessage assembled there
        # carries a generated id that a real provider rejects as an unpaired
        # tool_result. Given plain content, the tool node stamps the model's
        # own id — exactly as it does for unwrapped tools.
        with workflow.unsafe.imports_passed_through():
            from langchain_core.messages import ToolMessage

        if isinstance(message, ToolMessage):
            return message.content
        return message

    _ROUTED_TOOL_NAMES.add(tool_name)
    return StructuredTool(
        name=tool_name,
        description=base_tool.description,
        args_schema=base_tool.args_schema,  # type: ignore[arg-type]
        coroutine=_run,
    )


def _is_coroutine(fn: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# TemporalBackend
# ---------------------------------------------------------------------------

# Async protocol methods and their sync twins. deepagents' ``BackendProtocol``
# implements each async DEFAULT as ``asyncio.to_thread(sync_twin, ...)``; the
# deterministic workflow event loop has no thread executor, so any built-in
# tool call against an unwrapped in-workflow backend (e.g. the default
# ``StateBackend``) raises ``NotImplementedError``. A real model hits this on
# its first spontaneous ``grep``/``read_file`` call; scripted-model tests that
# never call built-ins sail past it.
_ASYNC_TO_SYNC_OPS: dict[str, str] = {
    "als": "ls",
    "als_info": "ls_info",
    "aread": "read",
    "awrite": "write",
    "aedit": "edit",
    "aglob": "glob",
    "aglob_info": "glob_info",
    "agrep": "grep",
    "agrep_raw": "grep_raw",
    "adownload_files": "download_files",
    "aupload_files": "upload_files",
    "aexecute": "execute",
}

_original_backend_async_defaults: dict[str, Any] = {}


def install_backend_async_patch() -> None:
    """Make ``BackendProtocol``'s async defaults workflow-safe.

    Inside a workflow, run the sync twin inline: for state-only backends that
    is deterministic and semantically identical to the upstream default,
    which merely moves the same sync call onto a worker thread. Outside a
    workflow (activities, clients) the upstream default — thread hop plus
    timeout guard — is used unchanged. Subclasses that override an async
    method natively are unaffected; only the protocol defaults are replaced.
    Idempotent.
    """
    # importlib: `deepagents` is absent on Python 3.10 environments (its floor
    # is 3.11), so a static import here fails type-checking there.
    BackendProtocol = importlib.import_module(
        "deepagents.backends.protocol"
    ).BackendProtocol

    if _original_backend_async_defaults:
        return
    for async_name, sync_name in _ASYNC_TO_SYNC_OPS.items():
        original = BackendProtocol.__dict__.get(async_name)
        if original is None:
            continue

        def _make(sync_name: str, original: Any) -> Any:
            async def patched(self: Any, *args: Any, **kwargs: Any) -> Any:
                if workflow.in_workflow():
                    return getattr(self, sync_name)(*args, **kwargs)
                return await original(self, *args, **kwargs)

            return patched

        _original_backend_async_defaults[async_name] = original
        setattr(BackendProtocol, async_name, _make(sync_name, original))


def uninstall_backend_async_patch() -> None:
    """Restore ``BackendProtocol``'s upstream async defaults."""
    if not _original_backend_async_defaults:
        return
    BackendProtocol = importlib.import_module(
        "deepagents.backends.protocol"
    ).BackendProtocol

    for async_name, original in _original_backend_async_defaults.items():
        setattr(BackendProtocol, async_name, original)
    _original_backend_async_defaults.clear()


# Backend METHOD names (deepagents ``BackendProtocol`` +
# ``SandboxBackendProtocol``) whose calls must cross the activity boundary:
# every sync I/O method, its async ``a``-prefixed twin, and the sandbox/shell
# execute pair. The async twins are load-bearing — deepagents' filesystem
# middleware drives backends through ``als``/``aread``/``awrite``/… — so
# intercepting only sync names lets an agent's built-in file tools run I/O
# in-workflow. These are backend PROTOCOL method names, distinct from the
# LLM-facing tool names in ``_BUILTIN_TOOL_NAMES`` (``read_file`` etc.).
_BACKEND_OPS = (
    # Sync protocol surface.
    "ls",
    "ls_info",
    "read",
    "write",
    "edit",
    "glob",
    "glob_info",
    "grep",
    "grep_raw",
    "download_files",
    "upload_files",
    "execute",
    # Async twins (what FilesystemMiddleware actually calls).
    "als",
    "als_info",
    "aread",
    "awrite",
    "aedit",
    "aglob",
    "aglob_info",
    "agrep",
    "agrep_raw",
    "adownload_files",
    "aupload_files",
    "aexecute",
)


class TemporalBackend:
    """Route a real-I/O backend's operations through Temporal activities.

    Wrap a ``FilesystemBackend`` / ``LocalShellBackend`` / ``StoreBackend`` /
    ``CompositeBackend`` so each file or shell operation becomes a durable
    ``deepagents.backend_op`` activity instead of touching disk / shell from the
    workflow. State-only backends (``StateBackend``) need no wrapping — they are
    pure workflow state and run in-workflow.

    Unknown attribute access is forwarded to the inner backend so backend
    metadata / configuration the agent reads (but that does no I/O) still works.
    """

    def __init__(
        self,
        inner: Any,
        *,
        activity_options: Mapping[str, Any] | None = None,
    ) -> None:
        """Wrap ``inner`` so its I/O ops dispatch as durable activities."""
        self._inner = inner
        # A deterministic id: workflow.uuid4() is seeded per-run, so the ref is
        # identical across replays (unlike id(inner)). Falls back to a plain uuid
        # when a backend is wrapped outside a workflow (e.g. in a plain test).
        if workflow.in_workflow():
            self._ref = f"backend:{workflow.uuid4().hex}"
        else:
            self._ref = f"backend:{_uuid.uuid4().hex}"
        self._opts: dict[str, Any] = dict(activity_options or {})
        self._opts.setdefault("start_to_close_timeout", timedelta(minutes=1))
        register_backend(self._ref, inner)
        # Balance the registration when this wrapper is garbage-collected
        # (workflow completion / cache eviction) so per-run backends do not
        # accumulate in the worker-global registry. The finalizer captures
        # (ref, inner) — not ``self`` — so it cannot keep the wrapper alive,
        # and it only removes its own registration (see _unregister_backend).
        self._finalizer = weakref.finalize(self, _unregister_backend, self._ref, inner)

    async def _dispatch(self, op: str, *args: Any, **kwargs: Any) -> Any:
        from temporalio.contrib.deepagents.workflow import call_backend_op

        activity_input = _activity.BackendOpInput(
            backend_ref=self._ref,
            op=op,
            args=list(args),
            kwargs=dict(kwargs),
        )
        output = await call_backend_op(
            activity_input,
            summary=f"backend:{op}",
            **self._opts,
        )
        return _serde.load_backend_result(output.result)

    def __getattr__(self, name: str) -> Any:
        """Bound-method access for a known I/O op returns an activity dispatcher.

        Everything else forwards to the inner backend unchanged.
        """
        if name in _BACKEND_OPS:

            async def _op(*args: Any, **kwargs: Any) -> Any:
                return await self._dispatch(name, *args, **kwargs)

            return _op
        return getattr(self._inner, name)
