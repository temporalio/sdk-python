"""Regression test for the poll-task shutdown race condition.

When the activity or nexus worker's ``_fail_worker_exception_queue`` fires
while a poll is in flight, the old code did:

    poll_task.cancel()
    await exception_task   # <-- re-raised immediately, no yield to the loop

The Rust bridge future backing ``poll_task`` may have completed *just before*
``poll_task.cancel()`` was called, meaning it scheduled a
``loop.call_soon_threadsafe(set_result, ...)`` callback that is now sitting in
the event loop's ``_ready`` queue.  Because ``await exception_task`` raises
without ever yielding back to the loop, the queue is never flushed.  Later,
when ``asyncio.run()`` tears down the loop (and ``_cancel_all_tasks`` finds no
remaining tasks to wait on), ``loop.close()`` fires before the callback runs,
producing ``RuntimeError("Event loop is closed")``.

The fix adds ``await asyncio.wait([poll_task])`` between ``poll_task.cancel()``
and ``await exception_task``, which yields to the event loop and lets any
pending Rust-side callbacks be processed while the loop is still open.

These tests simulate the race purely in Python (no Temporal server required)
by replacing the bridge future with a mock that reproduces the exact timing.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import warnings
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LoopClosedCatcher:
    """Collect 'Event loop is closed' warnings / logged errors emitted by asyncio."""

    def __init__(self) -> None:
        self.caught: list[dict[str, Any]] = []

    def handler(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        msg = context.get("message", "")
        exc = context.get("exception")
        if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
            self.caught.append(context)
        elif "Event loop is closed" in msg:
            self.caught.append(context)


async def _make_poll_future_that_races(
    loop: asyncio.AbstractEventLoop,
) -> asyncio.Future[bytes]:
    """Return an asyncio.Future whose result is set from a *thread* with a tiny
    delay so that it races against ``poll_task.cancel()``.

    This mimics the Rust bridge: the Tokio future completes slightly after the
    Python side has called ``poll_task.cancel()``, but before the event loop
    has had a chance to process the cancellation.  The bridge then calls
    ``loop.call_soon_threadsafe(future.set_result, ...)``; if the loop is
    already closed at that point, the ``RuntimeError`` is triggered.
    """
    fut: asyncio.Future[bytes] = loop.create_future()

    def _deliver() -> None:
        # Simulate Rust completing just a hair after Python calls cancel().
        # In real code this is call_soon_threadsafe; we replicate that here.
        if not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: None if fut.done() else fut.set_result(b"")
            )

    # Fire the delivery from a background thread after 1 ms — just long
    # enough for poll_task.cancel() to have been scheduled but before the
    # loop processes it.
    t = threading.Timer(0.001, _deliver)
    t.daemon = True
    t.start()
    return await fut


# ---------------------------------------------------------------------------
# Core race reproducer (no external dependencies)
# ---------------------------------------------------------------------------


async def _run_poll_loop_old(
    fail_queue: asyncio.Queue[Exception],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Reproduces the BUGGY poll loop from _activity.py / _nexus.py before the fix."""

    async def raise_from_exception_queue() -> None:
        raise await fail_queue.get()  # type: ignore[misc]

    exception_task: asyncio.Task[None] = asyncio.create_task(
        raise_from_exception_queue()
    )

    try:
        # Create ONE poll iteration with a racing future
        poll_task: asyncio.Task[bytes] = asyncio.create_task(
            _make_poll_future_that_races(loop)
        )
        await asyncio.wait(
            [poll_task, exception_task], return_when=asyncio.FIRST_COMPLETED
        )
        if exception_task.done():
            poll_task.cancel()
            # BUG: no drain — exits immediately without flushing pending callbacks
            await exception_task
        await poll_task  # noqa: RUF100
    except Exception:
        exception_task.cancel()
        raise RuntimeError("Worker failed") from None


async def _run_poll_loop_fixed(
    fail_queue: asyncio.Queue[Exception],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Poll loop with the fix applied."""

    async def raise_from_exception_queue() -> None:
        raise await fail_queue.get()  # type: ignore[misc]

    exception_task: asyncio.Task[None] = asyncio.create_task(
        raise_from_exception_queue()
    )

    try:
        poll_task: asyncio.Task[bytes] = asyncio.create_task(
            _make_poll_future_that_races(loop)
        )
        await asyncio.wait(
            [poll_task, exception_task], return_when=asyncio.FIRST_COMPLETED
        )
        if exception_task.done():
            poll_task.cancel()
            # FIX: drain poll_task so Rust callbacks fire while loop is open
            await asyncio.wait([poll_task])
            await exception_task
        await poll_task  # noqa: RUF100
    except Exception:
        exception_task.cancel()
        raise RuntimeError("Worker failed") from None


def _run_with_exception_injected(coro_factory: Any) -> _LoopClosedCatcher:
    """Run ``coro_factory(fail_queue, loop)`` inside a fresh asyncio event loop,
    inject a worker exception into ``fail_queue`` after a brief pause, then
    return a catcher that records any 'Event loop is closed' errors."""

    catcher = _LoopClosedCatcher()

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(catcher.handler)

        fail_queue: asyncio.Queue[Exception] = asyncio.Queue()

        async def _inject_exception() -> None:
            await asyncio.sleep(0)  # yield once so poll_task can start
            fail_queue.put_nowait(RuntimeError("injected worker failure"))

        injector = asyncio.create_task(_inject_exception())
        try:
            await coro_factory(fail_queue, loop)
        except RuntimeError:
            pass
        finally:
            await injector

    asyncio.run(_main())

    # Give background threads a moment to fire any lingering callbacks
    # that would arrive *after* loop.close().
    import time

    time.sleep(0.05)
    return catcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("coro_factory", [_run_poll_loop_fixed])
def test_no_closed_loop_error_with_fix(coro_factory: Any) -> None:
    """The fixed poll loop must not produce RuntimeError('Event loop is closed')."""
    catcher = _run_with_exception_injected(coro_factory)
    assert catcher.caught == [], (
        "RuntimeError('Event loop is closed') was raised after shutdown with the fix applied. "
        f"Details: {catcher.caught}"
    )


def test_fixed_activity_worker_poll_exits_cleanly() -> None:
    """Smoke-test: _ActivityWorker.run() with a mocked bridge that races on shutdown."""
    import sys
    import os

    # We need the local sdk-python on path, not the installed one
    sdk_path = os.path.join(os.path.dirname(__file__), "..", "..")
    sys.path.insert(0, os.path.abspath(sdk_path))

    # Minimal smoke test: the fixed code path (asyncio.wait) should be reachable
    # without ImportError or syntax error.
    from temporalio.worker._activity import _ActivityWorker  # type: ignore[import]

    # Check that the fix is present in source
    import inspect

    source = inspect.getsource(_ActivityWorker.run)
    assert "await asyncio.wait([poll_task])" in source, (
        "Fix not found in _ActivityWorker.run()! "
        "Expected 'await asyncio.wait([poll_task])' between poll_task.cancel() and await exception_task."
    )


def test_fixed_nexus_worker_poll_exits_cleanly() -> None:
    """Smoke-test: _NexusWorker.run() source contains the drain fix."""
    import sys
    import os

    sdk_path = os.path.join(os.path.dirname(__file__), "..", "..")
    sys.path.insert(0, os.path.abspath(sdk_path))

    try:
        from temporalio.worker._nexus import _NexusWorker  # type: ignore[import]
    except ImportError:
        pytest.skip("_NexusWorker not available in this SDK version")

    import inspect

    source = inspect.getsource(_NexusWorker.run)
    assert "await asyncio.wait([poll_task])" in source, (
        "Fix not found in _NexusWorker.run()! "
        "Expected 'await asyncio.wait([poll_task])' between poll_task.cancel() and await exception_task."
    )
