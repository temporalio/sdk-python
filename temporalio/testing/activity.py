"""Activity test environment."""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Set, TypeVar

from typing_extensions import ParamSpec

import temporalio.activity

_Params = ParamSpec("_Params")
_Return = TypeVar("_Return")

_utc_zero = datetime.fromtimestamp(0).replace(tzinfo=timezone.utc)
_default_info = temporalio.activity.Info(
    activity_id="test",
    activity_type="unknown",
    attempt=1,
    current_attempt_scheduled_time=_utc_zero,
    heartbeat_details=[],
    heartbeat_timeout=None,
    is_local=False,
    schedule_to_close_timeout=timedelta(seconds=1),
    scheduled_time=_utc_zero,
    start_to_close_timeout=timedelta(seconds=1),
    started_time=_utc_zero,
    task_queue="test",
    task_token=b"test",
    workflow_id="test",
    workflow_namespace="default",
    workflow_run_id="test-run",
    workflow_type="test",
)


class ActivityEnvironment:
    def __init__(self) -> None:
        self.info = _default_info
        self.on_heartbeat: Callable[..., None] = lambda *args: None
        self._cancelled = False
        self._worker_shutdown = False
        self._activities: Set[_Activity] = set()

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        for act in self._activities:
            act.cancel()

    def worker_shutdown(self) -> None:
        if self._worker_shutdown:
            return
        self._worker_shutdown = True
        for act in self._activities:
            act.worker_shutdown()

    def run(
        self,
        fn: Callable[_Params, _Return],
        *args: _Params.args,
        **kwargs: _Params.kwargs,
    ) -> _Return:
        # Create an activity and run it
        return _Activity(self, fn).run(*args, **kwargs)


class _Activity:
    def __init__(
        self,
        env: ActivityEnvironment,
        fn: Callable,
    ) -> None:
        self.env = env
        self.fn = fn
        self.is_async = inspect.iscoroutinefunction(fn)
        # Create context
        self.context = temporalio.activity._Context(
            info=lambda: env.info,
            heartbeat=lambda *args: env.on_heartbeat(*args),
            cancelled_event=temporalio.activity._CompositeEvent(
                thread_event=threading.Event(),
                async_event=asyncio.Event() if self.is_async else None,
            ),
            worker_shutdown_event=temporalio.activity._CompositeEvent(
                thread_event=threading.Event(),
                async_event=asyncio.Event() if self.is_async else None,
            ),
        )
        self.task: Optional[asyncio.Task] = None

    def run(self, *args, **kwargs) -> Any:
        @contextmanager
        def activity_context():
            # Set cancelled and shutdown if already so in environment
            if self.env._cancelled:
                self.context.cancelled_event.set()
            if self.env._worker_shutdown:
                self.context.worker_shutdown_event.set()

            # Add activity and set context
            self.env._activities.add(self)
            token = temporalio.activity._Context.set(self.context)
            try:
                yield None
            finally:
                # Reset context and remove activity
                temporalio.activity._Context.reset(token)
                self.env._activities.remove(self)

        # Async runs inside coroutine with a cancellable task
        if self.is_async:

            async def run_async():
                with activity_context():
                    self.task = asyncio.create_task(self.fn(*args, **kwargs))
                    if self.env._cancelled:
                        self.task.cancel()
                    return await self.task

            return run_async()
        # Sync just runs normally
        with activity_context():
            return self.fn(*args, **kwargs)

    def cancel(self) -> None:
        if not self.context.cancelled_event.is_set():
            self.context.cancelled_event.set()
        if self.task and not self.task.done():
            self.task.cancel()

    def worker_shutdown(self) -> None:
        if not self.context.worker_shutdown_event.is_set():
            self.context.worker_shutdown_event.set()
