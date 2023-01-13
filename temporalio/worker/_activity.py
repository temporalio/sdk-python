"""Activity worker."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import multiprocessing
import multiprocessing.managers
import pickle
import queue
import threading
import warnings
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

import google.protobuf.duration_pb2
import google.protobuf.timestamp_pb2

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.common
import temporalio.bridge.runtime
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions

from ._interceptor import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

logger = logging.getLogger(__name__)


class _ActivityWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        task_queue: str,
        activities: Sequence[Callable],
        activity_executor: Optional[concurrent.futures.Executor],
        shared_state_manager: Optional[SharedStateManager],
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
    ) -> None:
        self._bridge_worker = bridge_worker
        self._task_queue = task_queue
        self._activity_executor = activity_executor
        self._shared_state_manager = shared_state_manager
        self._running_activities: Dict[bytes, _RunningActivity] = {}
        self._data_converter = data_converter
        self._interceptors = interceptors
        self._fail_worker_exception_queue: asyncio.Queue[Exception] = asyncio.Queue()
        # Lazily created on first activity
        self._worker_shutdown_event: Optional[
            temporalio.activity._CompositeEvent
        ] = None
        self._seen_sync_activity = False

        # Validate and build activity dict
        self._activities: Dict[str, temporalio.activity._Definition] = {}
        for activity in activities:
            # Get definition
            defn = temporalio.activity._Definition.must_from_callable(activity)
            # Confirm name unique
            if defn.name in self._activities:
                raise ValueError(f"More than one activity named {defn.name}")

            # Some extra requirements for sync functions
            if not defn.is_async:
                if not activity_executor:
                    raise ValueError(
                        f"Activity {defn.name} is not async so an activity_executor must be present"
                    )
                if (
                    not isinstance(
                        activity_executor, concurrent.futures.ThreadPoolExecutor
                    )
                    and not shared_state_manager
                ):
                    raise ValueError(
                        f"Activity {defn.name} is not async and executor is not thread-pool executor, "
                        "so a shared_state_manager must be present"
                    )
                if isinstance(
                    activity_executor, concurrent.futures.ProcessPoolExecutor
                ):
                    # The function must be picklable for use in process
                    # executors, we we perform this eager check to fail at
                    # registration time
                    # TODO(cretz): Is this too expensive/unnecessary?
                    try:
                        pickle.dumps(activity)
                    except Exception as err:
                        raise TypeError(
                            f"Activity {defn.name} must be picklable when using a process executor"
                        ) from err
            self._activities[defn.name] = defn

    async def run(self) -> None:
        # Create a task that fails when we get a failure on the queue
        async def raise_from_queue() -> NoReturn:
            raise await self._fail_worker_exception_queue.get()

        exception_task = asyncio.create_task(raise_from_queue())

        # Continually poll for activity work
        while True:
            try:
                # Poll for a task
                poll_task = asyncio.create_task(
                    self._bridge_worker().poll_activity_task()
                )
                await asyncio.wait([poll_task, exception_task], return_when=asyncio.FIRST_COMPLETED)  # type: ignore
                # If exception for failing the worker happened, raise it.
                # Otherwise, the poll succeeded.
                if exception_task.done():
                    poll_task.cancel()
                    await exception_task
                task = await poll_task

                if task.HasField("start"):
                    # Cancelled event and sync field will be updated inside
                    # _run_activity when the activity function is obtained. Max
                    # size of 1000 should be plenty for the heartbeat queue.
                    activity = _RunningActivity(pending_heartbeats=asyncio.Queue(1000))
                    activity.task = asyncio.create_task(
                        self._run_activity(task.task_token, task.start, activity)
                    )
                    self._running_activities[task.task_token] = activity
                elif task.HasField("cancel"):
                    self._cancel(task.task_token, task.cancel)
                else:
                    raise RuntimeError(f"Unrecognized activity task: {task}")
            except temporalio.bridge.worker.PollShutdownError:
                exception_task.cancel()
                return
            except Exception as err:
                exception_task.cancel()
                raise RuntimeError("Activity worker failed") from err

    async def shutdown(self, after_graceful_timeout: timedelta) -> None:
        # Set event that we're shutting down (updates all activity tasks)
        if self._worker_shutdown_event:
            self._worker_shutdown_event.set()
        # Collect all still running activity tasks or exit if none
        activity_tasks = [
            activity.task
            for activity in self._running_activities.values()
            if activity.task
        ]
        if not activity_tasks:
            return
        # Wait for any still running after graceful timeout and exit if none
        _, still_running = await asyncio.wait(
            activity_tasks, timeout=after_graceful_timeout.total_seconds()
        )
        if not still_running:
            return
        # Cancel all still running
        if len(still_running) == 1:
            logger.info(f"Cancelling 1 activity that is still running")
        else:
            logger.info(
                f"Cancelling {len(still_running)} activities that are still running"
            )
        for task in still_running:
            # We have to find the running activity that's associated with
            # the task so that we can cancel through that. It's ok if the
            # activity is already gone.
            for activity in self._running_activities.values():
                if activity.info and activity.task is task:
                    logger.info(
                        f"Cancelling still-running activity: {activity.info._logger_details()}"
                    )
                    activity.cancel()
                    break
        # Now we have to wait on them to complete
        await asyncio.wait(still_running)

    def _cancel(
        self, task_token: bytes, cancel: temporalio.bridge.proto.activity_task.Cancel
    ) -> None:
        activity = self._running_activities.get(task_token)
        if not activity:
            warnings.warn(f"Cannot find activity to cancel for token {task_token!r}")
            return
        logger.debug("Cancelling activity %s, reason: %s", task_token, cancel.reason)
        activity.cancel(cancelled_by_request=True)

    def _heartbeat(self, task_token: bytes, *details: Any) -> None:
        # We intentionally make heartbeating non-async, but since the data
        # converter is async, we have to schedule it. If the activity is done,
        # we do not schedule any more. Technically this should be impossible to
        # call if the activity is done because this sync call can only be called
        # inside the activity and done is set to False when the activity
        # returns.
        logger = temporalio.activity.logger
        activity = self._running_activities.get(task_token)
        if activity and not activity.done:
            # Put on queue and schedule a task. We will let the queue-full error
            # be thrown here.
            activity.pending_heartbeats.put_nowait(details)
            activity.last_heartbeat_task = asyncio.create_task(
                self._heartbeat_async(logger, activity, task_token)
            )

    async def _heartbeat_async(
        self,
        logger: logging.LoggerAdapter,
        activity: _RunningActivity,
        task_token: bytes,
    ) -> None:
        # Drain the queue, only taking the last value to actually heartbeat
        details: Optional[Sequence[Any]] = None
        while not activity.pending_heartbeats.empty():
            details = activity.pending_heartbeats.get_nowait()
        if details is None:
            return

        # Perform the heartbeat
        try:
            heartbeat = temporalio.bridge.proto.ActivityHeartbeat(task_token=task_token)
            if details:
                # Convert to core payloads
                heartbeat.details.extend(await self._data_converter.encode(details))
            logger.debug("Recording heartbeat with details %s", details)
            self._bridge_worker().record_activity_heartbeat(heartbeat)
        except Exception as err:
            # If the activity is done, nothing we can do but log
            if activity.done:
                logger.exception(
                    "Failed recording heartbeat (activity already done, cannot error)"
                )
            else:
                logger.warning(
                    "Cancelling activity because failed recording heartbeat",
                    exc_info=True,
                )
                activity.cancel(cancelled_due_to_heartbeat_error=err)

    async def _run_activity(
        self,
        task_token: bytes,
        start: temporalio.bridge.proto.activity_task.Start,
        running_activity: _RunningActivity,
    ) -> None:
        logger.debug("Running activity %s (token %s)", start.activity_type, task_token)
        # We choose to surround interceptor creation and activity invocation in
        # a try block so we can mark the workflow as failed on any error instead
        # of having error handling in the interceptor
        completion = temporalio.bridge.proto.ActivityTaskCompletion(
            task_token=task_token
        )
        try:
            # Find activity or fail
            activity_def = self._activities.get(start.activity_type)
            if not activity_def:
                activity_names = ", ".join(sorted(self._activities.keys()))
                raise temporalio.exceptions.ApplicationError(
                    f"Activity function {start.activity_type} is not registered on this worker, available activities: {activity_names}",
                    type="NotFoundError",
                )

            # Create the worker shutdown event if not created
            if not self._worker_shutdown_event:
                self._worker_shutdown_event = temporalio.activity._CompositeEvent(
                    thread_event=threading.Event(), async_event=asyncio.Event()
                )

            # Setup events
            if not activity_def.is_async:
                running_activity.sync = True
                # If we're in a thread-pool executor we can use threading events
                # otherwise we must use manager events
                if isinstance(
                    self._activity_executor, concurrent.futures.ThreadPoolExecutor
                ):
                    running_activity.cancelled_event = temporalio.activity._CompositeEvent(
                        thread_event=threading.Event(),
                        # No async event
                        async_event=None,
                    )
                    if not activity_def.no_thread_cancel_exception:
                        running_activity.cancel_thread_raiser = _ThreadExceptionRaiser()
                else:
                    manager = self._shared_state_manager
                    # Pre-checked on worker init
                    assert manager
                    running_activity.cancelled_event = temporalio.activity._CompositeEvent(
                        thread_event=manager.new_event(),
                        # No async event
                        async_event=None,
                    )
                    # We also must set the worker shutdown thread event to a
                    # manager event if this is the first sync event. We don't
                    # want to create if there never is a sync event.
                    if not self._seen_sync_activity:
                        self._worker_shutdown_event.thread_event = manager.new_event()
                # Say we've seen a sync activity
                self._seen_sync_activity = True
            else:
                # We have to set the async form of events
                running_activity.cancelled_event = temporalio.activity._CompositeEvent(
                    thread_event=threading.Event(),
                    async_event=asyncio.Event(),
                )

            # Convert arguments. We only use arg type hints if they match the
            # input count.
            arg_types = activity_def.arg_types
            if arg_types is not None and len(arg_types) != len(start.input):
                arg_types = None
            try:
                args = (
                    []
                    if not start.input
                    else await self._data_converter.decode(
                        start.input, type_hints=arg_types
                    )
                )
            except Exception as err:
                raise temporalio.exceptions.ApplicationError(
                    "Failed decoding arguments"
                ) from err

            # Convert heartbeat details
            # TODO(cretz): Allow some way to configure heartbeat type hinting?
            try:
                heartbeat_details = (
                    []
                    if not start.heartbeat_details
                    else await self._data_converter.decode(start.heartbeat_details)
                )
            except Exception as err:
                raise temporalio.exceptions.ApplicationError(
                    "Failed decoding heartbeat details", non_retryable=True
                ) from err

            # Build info
            info = temporalio.activity.Info(
                activity_id=start.activity_id,
                activity_type=start.activity_type,
                attempt=start.attempt,
                current_attempt_scheduled_time=_proto_to_datetime(
                    start.current_attempt_scheduled_time
                ),
                heartbeat_details=heartbeat_details,
                heartbeat_timeout=_proto_to_non_zero_timedelta(start.heartbeat_timeout)
                if start.HasField("heartbeat_timeout")
                else None,
                is_local=start.is_local,
                schedule_to_close_timeout=_proto_to_non_zero_timedelta(
                    start.schedule_to_close_timeout
                )
                if start.HasField("schedule_to_close_timeout")
                else None,
                scheduled_time=_proto_to_datetime(start.scheduled_time),
                start_to_close_timeout=_proto_to_non_zero_timedelta(
                    start.start_to_close_timeout
                )
                if start.HasField("start_to_close_timeout")
                else None,
                started_time=_proto_to_datetime(start.started_time),
                task_queue=self._task_queue,
                task_token=task_token,
                workflow_id=start.workflow_execution.workflow_id,
                workflow_namespace=start.workflow_namespace,
                workflow_run_id=start.workflow_execution.run_id,
                workflow_type=start.workflow_type,
            )
            running_activity.info = info
            input = ExecuteActivityInput(
                fn=activity_def.fn,
                args=args,
                executor=None if not running_activity.sync else self._activity_executor,
                headers=start.header_fields,
            )

            # Set the context early so the logging adapter works and
            # interceptors have it
            temporalio.activity._Context.set(
                temporalio.activity._Context(
                    info=lambda: info,
                    heartbeat=None,
                    cancelled_event=running_activity.cancelled_event,
                    worker_shutdown_event=self._worker_shutdown_event,
                    shield_thread_cancel_exception=None
                    if not running_activity.cancel_thread_raiser
                    else running_activity.cancel_thread_raiser.shielded,
                )
            )
            temporalio.activity.logger.debug("Starting activity")

            # Build the interceptors chaining in reverse. We build a context right
            # now even though the info() can't be intercepted and heartbeat() will
            # fail. The interceptors may want to use the info() during init.
            impl: ActivityInboundInterceptor = _ActivityInboundImpl(
                self, running_activity
            )
            for interceptor in reversed(list(self._interceptors)):
                impl = interceptor.intercept_activity(impl)
            # Init
            impl.init(_ActivityOutboundImpl(self, running_activity.info))
            # Exec
            result = await impl.execute_activity(input)
            # Convert result even if none. Since Python essentially only
            # supports single result types (even if they are tuples), we will do
            # the same.
            completion.result.completed.result.CopyFrom(
                (await self._data_converter.encode([result]))[0]
            )
        except (
            Exception,
            asyncio.CancelledError,
            temporalio.exceptions.CancelledError,
            temporalio.activity._CompleteAsyncError,
        ) as err:
            try:
                if isinstance(err, temporalio.activity._CompleteAsyncError):
                    temporalio.activity.logger.debug("Completing asynchronously")
                    completion.result.will_complete_async.SetInParent()
                elif (
                    isinstance(
                        err,
                        (asyncio.CancelledError, temporalio.exceptions.CancelledError),
                    )
                    and running_activity.cancelled_due_to_heartbeat_error
                ):
                    err = running_activity.cancelled_due_to_heartbeat_error
                    temporalio.activity.logger.warning(
                        f"Completing as failure during heartbeat with error of type {type(err)}: {err}",
                    )
                    await self._data_converter.encode_failure(
                        err, completion.result.failed.failure
                    )
                elif (
                    isinstance(
                        err,
                        (asyncio.CancelledError, temporalio.exceptions.CancelledError),
                    )
                    and running_activity.cancelled_by_request
                ):
                    temporalio.activity.logger.debug("Completing as cancelled")
                    await self._data_converter.encode_failure(
                        # TODO(cretz): Should use some other message?
                        temporalio.exceptions.CancelledError("Cancelled"),
                        completion.result.cancelled.failure,
                    )
                else:
                    temporalio.activity.logger.warning(
                        "Completing activity as failed", exc_info=True
                    )
                    # In some cases, like worker shutdown of an sync activity,
                    # this results in a CancelledError, but the server will fail
                    # if you send a cancelled error outside of a requested
                    # cancellation. So we wrap as a retryable application error.
                    if isinstance(
                        err,
                        (asyncio.CancelledError, temporalio.exceptions.CancelledError),
                    ):
                        new_err = temporalio.exceptions.ApplicationError(
                            "Cancelled without request, possibly due to worker shutdown",
                            type="CancelledError",
                        )
                        new_err.__traceback__ = err.__traceback__
                        new_err.__cause__ = err.__cause__
                        err = new_err
                    await self._data_converter.encode_failure(
                        err, completion.result.failed.failure
                    )

                    # For broken executors, we have to fail the entire worker
                    if isinstance(err, concurrent.futures.BrokenExecutor):
                        self._fail_worker_exception_queue.put_nowait(err)
            except Exception as inner_err:
                temporalio.activity.logger.exception(
                    f"Exception handling failed, original error: {err}"
                )
                completion.result.Clear()
                completion.result.failed.failure.message = (
                    f"Failed building exception result: {inner_err}"
                )

        # Do final completion
        try:
            # We mark the activity as done and let the currently running
            # heartbeat task finish
            running_activity.done = True
            if running_activity.last_heartbeat_task:
                try:
                    await running_activity.last_heartbeat_task
                except:
                    # Should never happen because it's trapped in-task
                    temporalio.activity.logger.exception(
                        "Final heartbeat task didn't trap error"
                    )

            # Send task completion to core
            logger.debug("Completing activity with completion: %s", completion)
            await self._bridge_worker().complete_activity_task(completion)
            del self._running_activities[task_token]
        except Exception:
            temporalio.activity.logger.exception("Failed completing activity task")


@dataclass
class _RunningActivity:
    pending_heartbeats: asyncio.Queue[Sequence[Any]]
    # Most of these optional values are set before use
    info: Optional[temporalio.activity.Info] = None
    task: Optional[asyncio.Task] = None
    cancelled_event: Optional[temporalio.activity._CompositeEvent] = None
    last_heartbeat_task: Optional[asyncio.Task] = None
    cancel_thread_raiser: Optional[_ThreadExceptionRaiser] = None
    sync: bool = False
    done: bool = False
    cancelled_by_request: bool = False
    cancelled_due_to_heartbeat_error: Optional[Exception] = None

    def cancel(
        self,
        *,
        cancelled_by_request: bool = False,
        cancelled_due_to_heartbeat_error: Optional[Exception] = None,
    ) -> None:
        self.cancelled_by_request = cancelled_by_request
        self.cancelled_due_to_heartbeat_error = cancelled_due_to_heartbeat_error
        if self.cancelled_event:
            self.cancelled_event.set()
        if not self.done:
            # If there's a thread raiser, use it
            if self.cancel_thread_raiser:
                self.cancel_thread_raiser.raise_in_thread(
                    temporalio.exceptions.CancelledError
                )
            # If not sync and there's a task, cancel it
            if not self.sync and self.task:
                # TODO(cretz): Check that Python >= 3.9 and set msg?
                self.task.cancel()


class _ThreadExceptionRaiser:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread_id: Optional[int] = None
        self._pending_exception: Optional[Type[Exception]] = None
        self._shield_depth = 0

    def set_thread_id(self, thread_id: int) -> None:
        with self._lock:
            self._thread_id = thread_id

    def raise_in_thread(self, exc_type: Type[Exception]) -> None:
        with self._lock:
            self._pending_exception = exc_type
            self._raise_in_thread_if_pending_unlocked()

    @contextmanager
    def shielded(self) -> Iterator[None]:
        with self._lock:
            self._shield_depth += 1
        try:
            yield None
        finally:
            with self._lock:
                self._shield_depth -= 1
                self._raise_in_thread_if_pending_unlocked()

    def _raise_in_thread_if_pending_unlocked(self) -> None:
        # Does not apply if no thread ID
        if self._thread_id is not None:
            # Raise and reset if depth is 0
            if self._shield_depth == 0 and self._pending_exception:
                temporalio.bridge.runtime.Runtime._raise_in_thread(
                    self._thread_id, self._pending_exception
                )
                self._pending_exception = None


class _ActivityInboundImpl(ActivityInboundInterceptor):
    def __init__(
        self, worker: _ActivityWorker, running_activity: _RunningActivity
    ) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._running_activity = running_activity

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        # Set the context callables. We are setting values instead of replacing
        # the context just in case other interceptors held a reference.
        context = temporalio.activity._Context.current()
        context.info = outbound.info
        context.heartbeat = outbound.heartbeat

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        # Handle synchronous activity
        is_async = inspect.iscoroutinefunction(input.fn) or inspect.iscoroutinefunction(input.fn.__call__)  # type: ignore
        if not is_async:
            # We execute a top-level function via the executor. It is top-level
            # because it needs to be picklable. Also, by default Python does not
            # propagate contextvars into executor futures so we don't either
            # with the obvious exception of our context (if they want more, they
            # can set the initializer on the executor).
            ctx = temporalio.activity._Context.current()
            info = ctx.info()

            # Heartbeat calls internally use a data converter which is async so
            # they need to be called on the event loop
            loop = asyncio.get_running_loop()
            orig_heartbeat = ctx.heartbeat

            # We have to call the heartbeat function inside the asyncio event
            # loop (even though it's sync). So we need a call that puts the
            # context back on the activity and calls heartbeat, then another
            # call schedules it.
            async def heartbeat_with_context(*details: Any) -> None:
                temporalio.activity._Context.set(ctx)
                assert orig_heartbeat
                orig_heartbeat(*details)

            # Invoke the async heartbeat waiting a max of 10 seconds for
            # accepting
            ctx.heartbeat = lambda *details: asyncio.run_coroutine_threadsafe(
                heartbeat_with_context(*details), loop
            ).result(10)

            # For heartbeats, we use the existing heartbeat callable for thread
            # pool executors or a multiprocessing queue for others
            heartbeat: Union[Callable[..., None], SharedHeartbeatSender] = ctx.heartbeat
            shared_manager: Optional[SharedStateManager] = None
            if not isinstance(input.executor, concurrent.futures.ThreadPoolExecutor):
                # Should always be present in worker, pre-checked on init
                shared_manager = self._worker._shared_state_manager
                assert shared_manager
                heartbeat = await shared_manager.register_heartbeater(
                    info.task_token, ctx.heartbeat
                )

            try:
                # Cancel and shutdown event always present here
                cancelled_event = self._running_activity.cancelled_event
                assert cancelled_event
                worker_shutdown_event = self._worker._worker_shutdown_event
                assert worker_shutdown_event
                return await loop.run_in_executor(
                    input.executor,
                    _execute_sync_activity,
                    info,
                    heartbeat,
                    self._running_activity.cancel_thread_raiser,
                    # Only thread event, this may cross a process boundary
                    cancelled_event.thread_event,
                    worker_shutdown_event.thread_event,
                    input.fn,
                    *input.args,
                )
            finally:
                if shared_manager:
                    await shared_manager.unregister_heartbeater(info.task_token)

        # Otherwise for async activity, just run
        return await input.fn(*input.args)


class _ActivityOutboundImpl(ActivityOutboundInterceptor):
    def __init__(self, worker: _ActivityWorker, info: temporalio.activity.Info) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._info = info

    def info(self) -> temporalio.activity.Info:
        return self._info

    def heartbeat(self, *details: Any) -> None:
        info = temporalio.activity.info()
        self._worker._heartbeat(info.task_token, *details)


# This has to be defined at the top-level to be picklable for process executors
def _execute_sync_activity(
    info: temporalio.activity.Info,
    heartbeat: Union[Callable[..., None], SharedHeartbeatSender],
    # This is only set for threaded activities
    cancel_thread_raiser: Optional[_ThreadExceptionRaiser],
    cancelled_event: threading.Event,
    worker_shutdown_event: threading.Event,
    fn: Callable[..., Any],
    *args: Any,
) -> Any:
    if cancel_thread_raiser:
        thread_id = threading.current_thread().ident
        if thread_id is not None:
            cancel_thread_raiser.set_thread_id(thread_id)
    heartbeat_fn: Callable[..., None]
    if isinstance(heartbeat, SharedHeartbeatSender):
        # To make mypy happy
        heartbeat_sender = heartbeat
        heartbeat_fn = lambda *details: heartbeat_sender.send_heartbeat(
            info.task_token, *details
        )
    else:
        heartbeat_fn = heartbeat
    temporalio.activity._Context.set(
        temporalio.activity._Context(
            info=lambda: info,
            heartbeat=heartbeat_fn,
            cancelled_event=temporalio.activity._CompositeEvent(
                thread_event=cancelled_event, async_event=None
            ),
            worker_shutdown_event=temporalio.activity._CompositeEvent(
                thread_event=worker_shutdown_event, async_event=None
            ),
            shield_thread_cancel_exception=None
            if not cancel_thread_raiser
            else cancel_thread_raiser.shielded,
        )
    )
    return fn(*args)


class SharedStateManager(ABC):
    """Base class for a shared state manager providing cross-process-safe
    primitives for use by activity executors.

    Cross-worker use of the shared state manager is encouraged.
    :py:meth:`create_from_multiprocessing` provides the commonly used
    implementation.
    """

    @staticmethod
    def create_from_multiprocessing(
        mgr: multiprocessing.managers.SyncManager,
        queue_poller_executor: Optional[concurrent.futures.Executor] = None,
    ) -> SharedStateManager:
        """Create a shared state manager from a multiprocessing manager.

        Args:
            mgr: Sync manager to create primitives from. This is usually
                :py:func:`multiprocessing.Manager`.
            queue_poller_executor: The executor used when running the
                synchronous heartbeat queue poller. This should be a
                :py:class:`concurrent.futures.ThreadPoolExecutor`. If unset, a
                thread pool executor is created with max-workers of 1.

        Returns:
            The shared state manager.
        """
        return _MultiprocessingSharedStateManager(
            mgr, queue_poller_executor or concurrent.futures.ThreadPoolExecutor(1)
        )

    @abstractmethod
    def new_event(self) -> threading.Event:
        """Create a threading.Event that can be used across processes."""
        raise NotImplementedError

    @abstractmethod
    async def register_heartbeater(
        self, task_token: bytes, heartbeat: Callable[..., None]
    ) -> SharedHeartbeatSender:
        """Register a heartbeat function.

        Args:
            task_token: Unique task token for the heartbeater.
            heartbeat: Function that should be called when the resulting sender
                is sent a heartbeat.

        Returns:
            A sender that can be pickled for use in another process.
        """
        raise NotImplementedError

    @abstractmethod
    async def unregister_heartbeater(self, task_token: bytes) -> None:
        """Unregisters a previously registered heartbeater for the task
        token. This should also flush any pending heartbeats.
        """
        raise NotImplementedError


class SharedHeartbeatSender(ABC):
    """Base class for a heartbeat sender that is picklable for use in another
    process.
    """

    @abstractmethod
    def send_heartbeat(self, task_token: bytes, *details: Any) -> None:
        """Send a heartbeat for the given task token and details."""
        raise NotImplementedError


# List used for details to say a heartbeat is complete
_multiprocess_heartbeat_complete = ["__temporal_heartbeat_complete__"]


class _MultiprocessingSharedStateManager(SharedStateManager):
    def __init__(
        self,
        mgr: multiprocessing.managers.SyncManager,
        queue_poller_executor: concurrent.futures.Executor,
    ) -> None:
        super().__init__()
        self._mgr = mgr
        self._queue_poller_executor = queue_poller_executor
        # 1000 in-flight heartbeats should be plenty
        self._heartbeat_queue: queue.Queue[Tuple[bytes, Sequence[Any]]] = mgr.Queue(
            1000
        )
        self._heartbeats: Dict[bytes, Callable[..., None]] = {}
        self._heartbeat_completions: Dict[bytes, Callable] = {}

    def new_event(self) -> threading.Event:
        return self._mgr.Event()

    async def register_heartbeater(
        self, task_token: bytes, heartbeat: Callable[..., None]
    ) -> SharedHeartbeatSender:
        self._heartbeats[task_token] = heartbeat
        # If just now non-empty, start processor
        if len(self._heartbeats) == 1:
            self._queue_poller_executor.submit(self._heartbeat_processor)
        return _MultiprocessingSharedHeartbeatSender(self._heartbeat_queue)

    async def unregister_heartbeater(self, task_token: bytes) -> None:
        # Put a callback on the queue and wait for it to happen
        loop = asyncio.get_running_loop()
        finish_event = asyncio.Event()
        self._heartbeat_completions[task_token] = lambda: loop.call_soon_threadsafe(
            finish_event.set
        )
        try:
            # We only give the queue a few seconds to have enough room
            self._heartbeat_queue.put(
                (task_token, _multiprocess_heartbeat_complete), True, 5
            )
            await finish_event.wait()
        finally:
            del self._heartbeat_completions[task_token]

    def _heartbeat_processor(self) -> None:
        while len(self._heartbeats) > 0:
            try:
                # The timeout here of 0.5 seconds is how long until we try
                # again. This timeout then is the max amount of time before this
                # processor can stop when there are no more activity heartbeats
                # registered.
                item = self._heartbeat_queue.get(True, 0.5)
                # If it's a completion, perform that and continue
                if item[1] == _multiprocess_heartbeat_complete:
                    del self._heartbeats[item[0]]
                    completion = self._heartbeat_completions.get(item[0])
                    if completion:
                        completion()
                    continue
                # We count on this being a _very_ cheap function
                fn = self._heartbeats.get(item[0])
                if fn:
                    fn(*item[1])
            except queue.Empty:
                pass
            except Exception:
                logger.exception("Failed during multiprocess queue poll for heartbeat")
                return


class _MultiprocessingSharedHeartbeatSender(SharedHeartbeatSender):
    def __init__(
        self, heartbeat_queue: queue.Queue[Tuple[bytes, Sequence[Any]]]
    ) -> None:
        super().__init__()
        self._heartbeat_queue = heartbeat_queue

    def send_heartbeat(self, task_token: bytes, *details: Any) -> None:
        # We do want to wait here to ensure it was put on the queue, and we'll
        # timeout after 30 seconds (should be plenty if the queue is being
        # properly processed)
        self._heartbeat_queue.put((task_token, details), True, 30)


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> datetime:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


def _proto_to_non_zero_timedelta(
    dur: google.protobuf.duration_pb2.Duration,
) -> Optional[timedelta]:
    if dur.nanos == 0 and dur.seconds == 0:
        return None
    return dur.ToTimedelta()
