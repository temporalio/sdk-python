"""Future implementation for LangGraph Functional API tasks in Temporal.

This module provides a future class that bridges LangGraph's SyncAsyncFuture
interface with Temporal's activity execution model.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generator, Generic, TypeVar, cast

if TYPE_CHECKING:
    from temporalio.contrib.langgraph._functional_models import TaskActivityOutput
    from temporalio.workflow import ActivityHandle

T = TypeVar("T")


class TemporalTaskFuture(Generic[T], concurrent.futures.Future[T]):
    """Future that wraps a Temporal activity handle for LangGraph task execution.

    This class implements the concurrent.futures.Future interface (for .result())
    and supports async awaiting (for 'await' syntax), making it compatible with
    LangGraph's SyncAsyncFuture expectations.

    Note: The sync .result() method will raise an error in workflow context
    since blocking is not allowed. Use 'await' instead.
    """

    def __init__(
        self,
        activity_handle: ActivityHandle[T] | None = None,
        on_result: Callable[[T], None] | None = None,
    ) -> None:
        """Initialize the future.

        Args:
            activity_handle: The Temporal activity handle to wrap.
            on_result: Optional callback to invoke when the result is obtained.
                       Used to cache results for continue-as-new.
        """
        # Don't call super().__init__() - we manage state ourselves
        self._activity_handle: ActivityHandle[T] | None = activity_handle
        self._on_result = on_result
        self._result_value: T | None = None
        self._exception: BaseException | None = None
        self._done = False

    def set_activity_handle(self, handle: ActivityHandle[T]) -> None:
        """Set the activity handle after construction."""
        self._activity_handle = handle

    def set_result(self, result: T) -> None:
        """Set the result value (called when activity completes)."""
        self._result_value = result
        self._done = True

    def set_exception(self, exception: BaseException | None) -> None:
        """Set an exception (called when activity fails)."""
        self._exception = exception
        self._done = True

    def done(self) -> bool:
        """Return True if the future is done."""
        return self._done

    def cancelled(self) -> bool:
        """Return True if the future was cancelled."""
        # We don't support cancellation through this interface
        return False

    def running(self) -> bool:
        """Return True if the future is running."""
        return not self._done and self._activity_handle is not None

    def result(self, timeout: float | None = None) -> T:
        """Get the result of the task.

        In workflow context, this will raise an error since blocking is not
        allowed. Use 'await' instead.

        In activity context or outside Temporal, this may block.
        """
        if self._done:
            if self._exception:
                raise self._exception
            return cast(T, self._result_value)

        # Check if we're in a workflow context
        try:
            from temporalio import workflow

            if workflow.unsafe.in_sandbox():
                raise RuntimeError(
                    "Cannot use .result() in Temporal workflow context. "
                    "Use 'await' instead: result = await my_task(x)"
                )
        except ImportError:
            pass

        # If we have an activity handle, try to get its result
        # This would block, which is only OK outside workflow context
        if self._activity_handle is not None:
            # This is a limitation - we can't easily block on async handle
            raise RuntimeError("Cannot block on activity result. Use 'await' instead.")

        raise RuntimeError("Future not properly initialized")

    def exception(self, timeout: float | None = None) -> BaseException | None:
        """Get the exception if one was raised."""
        if self._done:
            return self._exception
        return None

    def add_done_callback(self, fn: Any) -> None:
        """Add a callback to be called when the future completes."""
        # Not implemented for now
        raise NotImplementedError("Callbacks not supported")

    def __await__(self) -> Generator[Any, None, T]:
        """Support 'await' syntax by delegating to the activity handle."""
        if self._done:
            if self._exception:
                raise self._exception
            return cast(T, self._result_value)

        if self._activity_handle is not None:
            # Yield from the activity handle's awaitable
            # The activity returns TaskActivityOutput, we need to extract the result
            activity_output = yield from self._activity_handle.__await__()

            # Unwrap TaskActivityOutput to get the actual result
            from temporalio.contrib.langgraph._functional_models import (
                TaskActivityOutput,
            )

            if isinstance(activity_output, TaskActivityOutput):
                result = cast(T, activity_output.result)
            else:
                result = cast(T, activity_output)

            self._result_value = result
            self._done = True

            # Call the on_result callback if provided (for caching)
            if self._on_result is not None:
                self._on_result(result)

            return result

        raise RuntimeError("Future not properly initialized - no activity handle")


class InlineFuture(Generic[T], concurrent.futures.Future[T]):
    """Future for inline task execution (when tasks call other tasks in activities).

    This immediately resolves with the result, used when a task calls another
    task inside an activity context where we execute inline rather than
    scheduling a new activity.
    """

    def __init__(self, result: T | None = None, exception: BaseException | None = None):
        """Initialize with an immediate result or exception."""
        self._result_value = result
        self._exception = exception

    def done(self) -> bool:
        """Always done since result is immediate."""
        return True

    def cancelled(self) -> bool:
        """Never cancelled."""
        return False

    def running(self) -> bool:
        """Never running since already complete."""
        return False

    def result(self, timeout: float | None = None) -> T:
        """Get the result immediately."""
        if self._exception:
            raise self._exception
        return cast(T, self._result_value)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        """Get the exception if one was raised."""
        return self._exception

    def add_done_callback(self, fn: Any) -> None:
        """Not implemented."""
        raise NotImplementedError("Callbacks not supported")

    def __await__(self) -> Generator[Any, None, T]:
        """Immediately return the result."""
        if self._exception:
            raise self._exception
        # For an already-complete future, we need to yield once then return
        # This is a quirk of Python's generator-based async
        if False:
            yield  # Make this a generator
        return cast(T, self._result_value)
