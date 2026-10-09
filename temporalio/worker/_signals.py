"""Scoped SIGTERM handling for workers."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from types import FrameType
from typing import Any


class _SigtermHandler:
    _handlers: set[_SigtermHandler] = set()

    def __init__(
        self, shutdown_event: asyncio.Event, context_task: asyncio.Task | None
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = shutdown_event
        self._context_task = context_task
        self._requested = False

    def __enter__(self) -> None:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
        ):
            return
        handler = signal.getsignal(signal.SIGTERM)
        if handler == signal.SIG_DFL:
            # A Python handler lets us preserve asyncio's wakeup fd and signal
            # registrations without relying on event loop implementation details.
            signal.signal(signal.SIGTERM, _handle_sigterm)
        elif handler is not _handle_sigterm:
            return
        self._handlers.add(self)

    def __exit__(self, *args: Any) -> None:
        if self not in self._handlers:
            return
        self._handlers.remove(self)
        if not self._handlers and signal.getsignal(signal.SIGTERM) is _handle_sigterm:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def _request_shutdown(self, context_tasks: set[asyncio.Task]) -> None:
        if self not in self._handlers or self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        if self._context_task and self._context_task not in context_tasks:
            context_tasks.add(self._context_task)
            self._context_task.cancel()


def _handle_sigterm(_signum: int, _frame: FrameType | None) -> None:
    # Repeated signals must not interrupt an already-running graceful shutdown.
    context_tasks: set[asyncio.Task] = set()
    for handler in tuple(_SigtermHandler._handlers):
        if handler._requested:
            continue
        handler._requested = True
        handler._loop.call_soon_threadsafe(handler._request_shutdown, context_tasks)
