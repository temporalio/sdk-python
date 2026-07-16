"""Activity-side helpers shared by the LangChain-family plugins."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError


def auto_heartbeater(fn: Callable) -> Callable:
    """Heartbeat at half the configured ``heartbeat_timeout`` while ``fn`` runs.

    Long LLM calls (thinking mode, long context, streaming accumulation) can run
    well past a scheduler's patience; without a heartbeat Temporal would cancel
    them and surface a ``HeartbeatTimeoutError`` instead of the real problem.
    """

    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        heartbeat_timeout = activity.info().heartbeat_timeout
        beat_task: asyncio.Task | None = None
        if heartbeat_timeout:
            interval = heartbeat_timeout.total_seconds() / 2

            async def beat() -> None:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(interval)

            beat_task = asyncio.create_task(beat())
        try:
            return await fn(*args, **kwargs)
        finally:
            if beat_task is not None:
                beat_task.cancel()
                # Let the cancellation land before returning so no pending task
                # outlives the activity (a bare ``cancel()`` leaves the task to
                # be destroyed while pending if the loop shuts down first).
                # ``asyncio.wait`` never re-raises the task's CancelledError.
                await asyncio.wait([beat_task])

    return wrapped


def translate_api_error(exc: Exception) -> ApplicationError | None:
    """Map an LLM SDK HTTP error onto Temporal's retry contract.

    Works by duck typing so neither ``openai`` nor ``anthropic`` needs to be
    imported here: both expose ``status_code`` and ``response.headers``. Returns
    ``None`` when ``exc`` is not a recognizable HTTP status error, so the caller
    can fall through to its generic handling.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        return None
    headers: dict[str, Any] = {}
    response = getattr(exc, "response", None)
    if response is not None:
        headers = dict(getattr(response, "headers", {}) or {})
    # Case-insensitive header access.
    lower = {str(k).lower(): v for k, v in headers.items()}

    retryable = status in (408, 409, 429) or 500 <= status < 600
    should_retry = lower.get("x-should-retry")
    if should_retry == "false":
        retryable = False
    elif should_retry == "true":
        retryable = True

    delay_ms = lower.get("retry-after-ms")
    retry_after = lower.get("retry-after")
    next_delay: timedelta | None = None
    try:
        if delay_ms is not None:
            next_delay = timedelta(milliseconds=int(delay_ms))
        elif retry_after is not None:
            next_delay = timedelta(seconds=int(retry_after))
    except (TypeError, ValueError):
        next_delay = None

    return ApplicationError(
        str(exc),
        type=type(exc).__name__,
        non_retryable=not retryable,
        next_retry_delay=next_delay,
    )
