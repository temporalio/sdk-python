from __future__ import annotations

import contextvars
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, NoReturn, Optional

import temporalio.exceptions

# TODO(cretz): Use logging adapter
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Info:
    activity_id: str
    activity_type: str
    attempt: int
    heartbeat_details: Iterable[Any]
    heartbeat_timeout: Optional[timedelta]
    schedule_to_close_timeout: Optional[timedelta]
    scheduled_time: datetime
    start_to_close_timeout: Optional[timedelta]
    started_time: datetime
    task_queue: str
    task_token: bytes
    workflow_id: str
    workflow_namespace: str
    workflow_run_id: str
    workflow_type: str
    # TODO(cretz): Add headers, current_attempt_scheduled_time, retry_policy, and is_local


_current_context: contextvars.ContextVar[_Context] = contextvars.ContextVar("activity")


@dataclass
class _Context:
    info: Callable[[], Info]
    # This is optional because during interceptor init it is not present
    heartbeat: Optional[Callable[..., None]]
    cancelled_event: threading.Event

    @staticmethod
    def current() -> _Context:
        context = _current_context.get(None)
        if not context:
            raise RuntimeError("Not in activity context")
        return context

    @staticmethod
    def set(context: _Context) -> None:
        _current_context.set(context)


def in_activity() -> bool:
    return not _current_context.get(None) is None


def info() -> Info:
    return _Context.current().info()


def heartbeat(*details: Any):
    heartbeat_fn = _Context.current().heartbeat
    if not heartbeat_fn:
        raise RuntimeError("Can only execute heartbeat after interceptor init")
    heartbeat_fn(*details)


def cancelled() -> bool:
    return _Context.current().cancelled_event.is_set()


# TODO(cretz): Make it clear this is not async API
def wait_for_cancelled(timeout: Optional[float] = None):
    _Context.current().cancelled_event.wait(timeout)


def raise_complete_async() -> NoReturn:
    raise CompleteAsyncError()


class CompleteAsyncError(temporalio.exceptions.TemporalError):
    pass
