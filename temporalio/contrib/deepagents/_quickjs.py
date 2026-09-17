"""In-workflow execution of the ``langchain-quickjs`` code interpreter.

``langchain_quickjs.CodeInterpreterMiddleware`` gives a Deep Agent an ``eval``
tool that runs model-written JavaScript in a QuickJS VM. Upstream hosts that VM
on a dedicated OS thread per LangGraph thread
(``quickjs_rs.threading.ThreadWorker``) and hands results back to the caller's
loop through ``asyncio.wrap_future`` — that is, ``call_soon_threadsafe``. The
deterministic workflow event loop neither implements that nor runs outside an
activation, so an unmodified ``eval`` parks the workflow forever: no failure,
no deadlock report, the execution just never wakes up.

Nothing about the REPL needs the thread. ``quickjs_rs`` drives the VM
cooperatively on whatever loop awaits it, and the middleware's ``task()`` /
``tools.*`` bridges already run their callbacks directly when the calling loop
is the loop that invoked ``eval``. So inside a workflow the worker's hops are
made inline: JavaScript runs as workflow code, a sub-agent dispatched from
JavaScript runs in-workflow (its model calls are ``deepagents.invoke_model``
activities like any other sub-agent's), and a PTC tool call follows the tool's
own Workflow-vs-Activity wrapping. Outside a workflow upstream behavior is
untouched, and when ``quickjs_rs`` is not installed there is nothing to patch.
"""

from __future__ import annotations

import importlib
from typing import Any

from temporalio import workflow

_originals: dict[str, Any] = {}


def install_quickjs_inline_patch() -> None:
    """Run ``quickjs_rs`` worker hops inline while in a workflow. Idempotent."""
    try:
        threading_mod = importlib.import_module("quickjs_rs.threading")
    except ImportError:
        return
    if _originals:
        return
    worker_cls = threading_mod.ThreadWorker
    orig_run_sync = worker_cls.run_sync
    orig_run_async = worker_cls.run_async
    orig_ensure_started = worker_cls._ensure_started

    def run_sync(self: Any, coro: Any) -> Any:
        if not workflow.in_workflow():
            return orig_run_sync(self, coro)
        # The REPL's synchronous hops (context creation, snapshot, close) never
        # suspend; drive the coroutine to completion right here.
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        coro.close()
        raise RuntimeError(
            "quickjs-rs REPL work suspended on the synchronous in-workflow path"
        )

    async def run_async(self: Any, coro: Any) -> Any:
        if not workflow.in_workflow():
            return await orig_run_async(self, coro)
        return await coro

    def ensure_started(self: Any) -> None:
        if workflow.in_workflow():
            return
        orig_ensure_started(self)

    _originals.update(
        run_sync=orig_run_sync,
        run_async=orig_run_async,
        _ensure_started=orig_ensure_started,
    )
    worker_cls.run_sync = run_sync
    worker_cls.run_async = run_async
    worker_cls._ensure_started = ensure_started


def uninstall_quickjs_inline_patch() -> None:
    """Restore ``quickjs_rs``'s thread-based worker hops."""
    if not _originals:
        return
    worker_cls = importlib.import_module("quickjs_rs.threading").ThreadWorker
    for name, original in _originals.items():
        setattr(worker_cls, name, original)
    _originals.clear()
