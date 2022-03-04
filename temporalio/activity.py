"""Functions that can be called inside of activities."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
    NoReturn,
    Optional,
    Tuple,
)

import temporalio.exceptions


@dataclass(frozen=True)
class Info:
    """Information about the running activity.

    Retrieved inside an activity via :py:func:`info`.
    """

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
    # TODO(cretz): Consider putting identity on here for "worker_id" for logger


_current_context: contextvars.ContextVar[_Context] = contextvars.ContextVar("activity")


@dataclass
class _Context:
    info: Callable[[], Info]
    # This is optional because during interceptor init it is not present
    heartbeat: Optional[Callable[..., None]]
    cancelled_event: _CompositeEvent
    worker_shutdown_event: _CompositeEvent
    _logger_details: Optional[Mapping[str, Any]] = None

    @staticmethod
    def current() -> _Context:
        context = _current_context.get(None)
        if not context:
            raise RuntimeError("Not in activity context")
        return context

    @staticmethod
    def set(context: _Context) -> None:
        _current_context.set(context)

    @property
    def logger_details(self) -> Mapping[str, Any]:
        if self._logger_details is None:
            info = self.info()
            self._logger_details = {
                "activity_id": info.activity_id,
                "activity_type": info.activity_type,
                "attempt": info.attempt,
                "namespace": info.workflow_namespace,
                "task_queue": info.task_queue,
                "workflow_id": info.workflow_id,
                "workflow_run_id": info.workflow_run_id,
                "workflow_type": info.workflow_type,
            }
        return self._logger_details


@dataclass
class _CompositeEvent:
    thread_event: threading.Event
    async_event: Optional[asyncio.Event]

    def set(self) -> None:
        self.thread_event.set()
        if self.async_event:
            self.async_event.set()


def in_activity() -> bool:
    """Whether the current code is inside an activity.

    Returns:
        True if in an activity, False otherwise.
    """
    return not _current_context.get(None) is None


def info() -> Info:
    """Current activity's info.

    Returns:
        Info for the currently running activity.

    Raises:
        RuntimeError: When not in an activity.
    """
    return _Context.current().info()


def heartbeat(*details: Any) -> None:
    """Send a heartbeat for the current activity.

    Raises:
        RuntimeError: When not in an activity.
    """
    heartbeat_fn = _Context.current().heartbeat
    if not heartbeat_fn:
        raise RuntimeError("Can only execute heartbeat after interceptor init")
    heartbeat_fn(*details)


def cancelled() -> bool:
    """Whether this activity is cancelled.

    Returns:
        True if the activity has been cancelled, False otherwise.

    Raises:
        RuntimeError: When not in an activity.
    """
    return _Context.current().cancelled_event.thread_event.is_set()


async def wait_for_cancelled() -> None:
    """Asynchronously wait for this activity to be cancelled.

    Raises:
        RuntimeError: When not in an async activity.
    """
    async_event = _Context.current().cancelled_event.async_event
    if not async_event:
        raise RuntimeError("not in async activity")
    await async_event.wait()


def wait_for_cancelled_sync(timeout: Optional[float] = None) -> None:
    """Synchronously block while waiting for cancellation on this activity.

    This is essentially a wrapper around :py:meth:`threading.Event.wait`.

    Args:
        timeout: Max amount of time to wait for cancellation.

    Raises:
        RuntimeError: When not in an activity.
    """
    _Context.current().cancelled_event.thread_event.wait(timeout)


def worker_shutdown() -> bool:
    """Whether shutdown has been invoked on the worker.

    Returns:
        True if shutdown has been called on the worker, False otherwise.

    Raises:
        RuntimeError: When not in an activity.
    """
    return _Context.current().worker_shutdown_event.thread_event.is_set()


async def wait_for_worker_shutdown() -> None:
    """Asynchronously wait for shutdown to be called on the worker.

    Raises:
        RuntimeError: When not in an async activity.
    """
    async_event = _Context.current().worker_shutdown_event.async_event
    if not async_event:
        raise RuntimeError("not in async activity")
    await async_event.wait()


def wait_for_worker_shutdown_sync(timeout: Optional[float] = None) -> None:
    """Synchronously block while waiting for shutdown to be called on the
    worker.

    This is essentially a wrapper around :py:meth:`threading.Event.wait`.

    Args:
        timeout: Max amount of time to wait for shutdown to be called on the
            worker.

    Raises:
        RuntimeError: When not in an activity.
    """
    _Context.current().worker_shutdown_event.thread_event.wait(timeout)


def raise_complete_async() -> NoReturn:
    """Raise an error that says the activity will be completed
    asynchronously.
    """
    raise CompleteAsyncError()


class CompleteAsyncError(temporalio.exceptions.TemporalError):
    """Raised by an activity to signal it will be completed asynchronously."""

    pass


class LoggerAdapter(logging.LoggerAdapter):
    """Adapter that adds details to the log about the running activity.

    Attributes:
        activity_info_on_message: Boolean for whether a string representation of
            a dict of some activity info will be appended to each message.
            Default is True.
        activity_info_on_extra: Boolean for whether an ``activity_info`` value
            will be added to the ``extra`` dictionary, making it present on the
            ``LogRecord.__dict__`` for use by others.
    """

    def __init__(
        self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]
    ) -> None:
        """Create the logger adapter."""
        super().__init__(logger, extra)
        self.activity_info_on_message = True
        self.activity_info_on_extra = True

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Override to add activity details."""
        msg, kwargs = super().process(msg, kwargs)
        if self.activity_info_on_extra or self.activity_info_on_extra:
            context = _current_context.get(None)
            if context:
                if self.activity_info_on_message:
                    msg = f"{msg} ({context.logger_details})"
                if self.activity_info_on_extra:
                    # Extra can be absent or None, this handles both
                    extra = kwargs.get("extra", None) or {}
                    extra["activity_info"] = context.info()
                    kwargs["extra"] = extra
        return (msg, kwargs)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger


#: Logger that will have contextual activity details embedded.
logger = LoggerAdapter(logging.getLogger(__name__), None)
# TODO(cretz): Do we want to give this another name? In Python, often people
# call this an "adapter" because it doesn't have every method that a logger does
# (e.g. you can't set handlers or formatters on it). But from inside an
# activity, temporalio.activity.logger.debug("whatever") is clearer. I have
# exposed the "base_logger" property to provide the underlying logger as needed
# (which is already on the "logger" property, but not typed). There are some
# more advanced overrides we _could_ do but they are more brittle.
