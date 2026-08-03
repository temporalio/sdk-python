"""LangSmith ``aio_to_thread`` override shared by the LangChain-family plugins."""

from __future__ import annotations

from typing import Any, Callable

import temporalio.workflow

_installed = False


async def _temporal_aio_to_thread(
    default_aio_to_thread: Callable[..., Any],
    ctx: Any,
    func: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run LangSmith's ``aio_to_thread`` synchronously inside Temporal workflows.

    The ``@traceable`` decorator on async functions uses ``aio_to_thread()`` →
    ``loop.run_in_executor()`` for run setup/teardown. The Temporal workflow
    event loop does not support ``run_in_executor``. This override runs those
    functions synchronously on the workflow thread when inside a workflow,
    and delegates to the default implementation outside workflows.

    Registered via ``langsmith.set_runtime_overrides(aio_to_thread=...)``.
    """
    if not temporalio.workflow.in_workflow():
        return await default_aio_to_thread(ctx, func, *args, **kwargs)
    with temporalio.workflow.unsafe.sandbox_unrestricted():
        return ctx.run(func, *args, **kwargs)


def install_aio_to_thread_override() -> None:
    """Install the ``aio_to_thread`` override via LangSmith's official API.

    Safe to call multiple times and from multiple plugins; the override is
    installed once per process. It is deliberately never uninstalled:
    LangSmith exposes a single process-wide override slot (each
    ``set_runtime_overrides`` call replaces it wholesale), so resetting it on
    one worker's shutdown would strip a composed plugin's still-needed
    override. Leaving it installed is safe — the override defers to
    LangSmith's default thread hop whenever ``workflow.in_workflow()`` is
    false, so it is inert outside workflows.

    Raises whatever the lazy ``langsmith`` import or
    ``set_runtime_overrides`` call raises (e.g. ``ImportError`` when
    LangSmith is absent); the installed flag stays unset on failure so a
    later call can retry.
    """
    global _installed  # noqa: PLW0603
    if _installed:
        return
    import langsmith

    langsmith.set_runtime_overrides(aio_to_thread=_temporal_aio_to_thread)
    _installed = True
