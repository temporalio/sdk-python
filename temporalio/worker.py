from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import itertools
import logging
import multiprocessing
import multiprocessing.managers
import pickle
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
)

import google.protobuf.duration_pb2
import google.protobuf.timestamp_pb2
import typing_extensions

import temporalio.activity
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.activity_task
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow_service

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        task_queue: str,
        activities: Mapping[str, Callable] = {},
        activity_executor: Optional[concurrent.futures.Executor] = None,
        interceptors: Iterable[Interceptor] = [],
        max_cached_workflows: int = 0,
        max_outstanding_workflow_tasks: int = 100,
        max_outstanding_activities: int = 100,
        max_outstanding_local_activities: int = 100,
        max_concurrent_wft_polls: int = 5,
        nonsticky_to_sticky_poll_ratio: float = 0.2,
        max_concurrent_at_polls: int = 5,
        no_remote_activities: bool = False,
        sticky_queue_schedule_to_start_timeout: timedelta = timedelta(seconds=10),
        max_heartbeat_throttle_interval: timedelta = timedelta(seconds=60),
        default_heartbeat_throttle_interval: timedelta = timedelta(seconds=30),
        # Used and required for sync activities when the executor is not a
        # thread pool executor.
        shared_state_manager: Optional[SharedStateManager] = None,
        # Unless this is false, eval_str will be used when evaluating type-hints
        # which is needed on files doing "from __future__ import annotations".
        # One may want to disable this if it's causing errors.
        type_hint_eval_str: bool = True,
    ) -> None:
        # TODO(cretz): Support workflows
        if not activities:
            raise ValueError("At least one activity must be specified")
        # If there are any non-async activities, an executor is required
        self._activities: Dict[str, _ActivityDefinition] = {}
        for name, activity in activities.items():
            if not callable(activity):
                raise TypeError(f"Activity {name} is not callable")
            elif not activity.__code__:
                raise TypeError(f"Activity {name} does not have __code__")
            elif activity.__code__.co_kwonlyargcount:
                raise TypeError(f"Activity {name} cannot have keyword-only arguments")
            elif not inspect.iscoroutinefunction(activity):
                if not activity_executor:
                    raise ValueError(
                        f"Activity {name} is not async so an activity_executor must be present"
                    )
                if (
                    not isinstance(
                        activity_executor, concurrent.futures.ThreadPoolExecutor
                    )
                    and not shared_state_manager
                ):
                    raise ValueError(
                        f"Activity {name} is not async and executor is not thread-pool executor, "
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
                            f"Activity {name} must be picklable when using a process executor"
                        ) from err
            arg_types, ret_type = temporalio.converter._type_hints_from_func(
                activity, type_hint_eval_str
            )
            self._activities[name] = _ActivityDefinition(
                name=name, fn=activity, arg_types=arg_types, ret_type=ret_type
            )

        # Prepend applicable client interceptors to the given ones
        interceptors = itertools.chain(
            (i for i in client.config()["interceptors"] if isinstance(i, Interceptor)),
            interceptors,
        )

        # Extract the bridge workflow service. We try the service on the client
        # first, then we support a worker_workflow_service on the client's
        # service to return underlying service we can use.
        bridge_service: temporalio.workflow_service._BridgeWorkflowService
        if isinstance(
            client.service, temporalio.workflow_service._BridgeWorkflowService
        ):
            bridge_service = client.service
        elif hasattr(client.service, "worker_workflow_service"):
            bridge_service = client.service.worker_workflow_service
            if not isinstance(
                bridge_service, temporalio.workflow_service._BridgeWorkflowService
            ):
                raise TypeError(
                    "Client service's worker_workflow_service cannot be used for a worker"
                )
        else:
            raise TypeError(
                "Client service cannot be used for a worker. "
                + "Use the original client's service or set worker_workflow_service on the wrapped service with the original service."
            )

        self._bridge_worker = temporalio.bridge.worker.Worker(
            bridge_service._bridge_client,
            temporalio.bridge.worker.WorkerConfig(
                namespace=client.namespace,
                task_queue=task_queue,
                max_cached_workflows=max_cached_workflows,
                max_outstanding_workflow_tasks=max_outstanding_workflow_tasks,
                max_outstanding_activities=max_outstanding_activities,
                max_outstanding_local_activities=max_outstanding_local_activities,
                max_concurrent_wft_polls=max_concurrent_wft_polls,
                nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
                max_concurrent_at_polls=max_concurrent_at_polls,
                no_remote_activities=no_remote_activities,
                sticky_queue_schedule_to_start_timeout_millis=int(
                    1000 * sticky_queue_schedule_to_start_timeout.total_seconds()
                ),
                max_heartbeat_throttle_interval_millis=int(
                    1000 * max_heartbeat_throttle_interval.total_seconds()
                ),
                default_heartbeat_throttle_interval_millis=int(
                    1000 * default_heartbeat_throttle_interval.total_seconds()
                ),
            ),
        )
        # Store the config for tracking
        self._config = WorkerConfig(
            client=client,
            task_queue=task_queue,
            activities=activities,
            activity_executor=activity_executor,
            interceptors=interceptors,
            max_cached_workflows=max_cached_workflows,
            max_outstanding_workflow_tasks=max_outstanding_workflow_tasks,
            max_outstanding_activities=max_outstanding_activities,
            max_outstanding_local_activities=max_outstanding_local_activities,
            max_concurrent_wft_polls=max_concurrent_wft_polls,
            nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
            max_concurrent_at_polls=max_concurrent_at_polls,
            no_remote_activities=no_remote_activities,
            sticky_queue_schedule_to_start_timeout=sticky_queue_schedule_to_start_timeout,
            max_heartbeat_throttle_interval=max_heartbeat_throttle_interval,
            default_heartbeat_throttle_interval=default_heartbeat_throttle_interval,
            shared_state_manager=shared_state_manager,
            type_hint_eval_str=type_hint_eval_str,
        )
        self._running_activities: Dict[bytes, _RunningActivity] = {}
        self._task: Optional[asyncio.Task] = None

    def config(self) -> WorkerConfig:
        """Config, as a dictionary, used to create this worker.

        This makes a shallow copy of the config each call.
        """
        config = self._config.copy()
        config["activities"] = dict(config["activities"])
        return config

    @property
    def task_queue(self) -> str:
        return self._config["task_queue"]

    async def __aenter__(self) -> Worker:
        self._start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.shutdown()

    async def run(self) -> None:
        await self._start()

    def _start(self) -> asyncio.Task:
        if self._task:
            raise RuntimeError("Already started")
        self._task = asyncio.create_task(
            asyncio.wait([asyncio.create_task(self._run_activities())])
        )
        return self._task

    async def shutdown(self) -> None:
        if not self._task:
            raise RuntimeError("Never started")
        await self._bridge_worker.shutdown()
        await self._task

    async def _run_activities(self) -> None:
        # Continually poll for activity work
        while True:
            try:
                # Poll for a task
                task = await self._bridge_worker.poll_activity_task()

                if task.HasField("start"):
                    # Cancelled event and sync field will be updated inside
                    # _run_activity when the activity function is obtained
                    activity = _RunningActivity(
                        task=None, cancelled_event=threading.Event(), sync=False
                    )
                    activity.task = asyncio.create_task(
                        self._run_activity(task.task_token, task.start, activity)
                    )
                    self._running_activities[task.task_token] = activity
                elif task.HasField("cancel"):
                    self._cancel_activity(task.task_token, task.cancel)
                else:
                    logger.warning("unrecognized activity task: %s", task)
            except temporalio.bridge.worker.PollShutdownError:
                return
            except Exception as err:
                # Should never happen
                logger.exception(f"Activity runner failed: {err}", extra={"error": err})

    async def _run_workflows(self) -> None:
        raise NotImplementedError

    def _cancel_activity(
        self, task_token: bytes, cancel: temporalio.bridge.proto.activity_task.Cancel
    ) -> None:
        activity = self._running_activities.get(task_token)
        if not activity:
            logger.warning("Cannot find activity to cancel for token %s", task_token)
            return
        logger.debug("Cancelling activity %s, reason: %s", task_token, cancel.reason)
        # TODO(cretz): Check that Python >= 3.9 and set msg?
        activity.cancel()

    def _heartbeat_activity(self, task_token: bytes, *details: Any) -> None:
        # We intentionally make heartbeating non-async, but since the data
        # converter is async, we have to schedule it
        logger = temporalio.activity.logger
        asyncio.create_task(
            self._heartbeat_activity_async(logger, task_token, *details)
        )

    async def _heartbeat_activity_async(
        self, logger: logging.Logger, task_token: bytes, *details: Any
    ) -> None:
        try:
            heartbeat = temporalio.bridge.proto.ActivityHeartbeat(task_token=task_token)
            if details:
                heartbeat.details.extend(
                    await self._config["client"].data_converter.encode(details)
                )
            # Since this is called async on the event loop, it is possible that
            # this is reached _after_ a sync activity has completed and removed
            # itself from the dict since, so we check it first
            if task_token in self._running_activities:
                self._bridge_worker.record_activity_heartbeat(heartbeat)
        except Exception as err:
            # Since this exception cannot be captured by the user, we will log
            # and cancel the activity
            logger.exception(
                "Cancelling activity because failed recording heartbeat: {err}",
                extra={"error": err},
            )
            activity = self._running_activities.get(task_token)
            if activity:
                activity.cancel()

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
                activity_names = ", ".join(sorted(self._config["activities"].keys()))
                raise temporalio.exceptions.ApplicationError(
                    f"Activity function {start.activity_type} is not registered on this worker, available activities: {activity_names}",
                    type="NotFoundError",
                    non_retryable=True,
                )

            # We must mark the running activity as sync if it's not async so
            # that cancellation doesn't attempt to cancel the task
            if not inspect.iscoroutinefunction(activity_def.fn):
                running_activity.sync = True

            # As a special case, if we're neither a coroutine or a thread pool
            # executor, we use a shared state manager event so it can span
            # processes just in case they are using some kind of multiprocess
            # executor
            if not inspect.iscoroutinefunction(activity_def.fn) and not isinstance(
                self._config["activity_executor"], concurrent.futures.ThreadPoolExecutor
            ):
                manager = self._config["shared_state_manager"]
                # Pre-checked on worker init
                assert manager
                running_activity.cancelled_event = manager.new_cancellation_event()

            # Convert arguments. We only use arg type hints if they match the
            # input count.
            arg_types = activity_def.arg_types
            if activity_def.arg_types is not None and len(
                activity_def.arg_types
            ) != len(start.input):
                arg_types = None
            converter = self._config["client"].data_converter
            try:
                args = (
                    []
                    if not start.input
                    else await converter.decode(start.input, type_hints=arg_types)
                )
            except Exception as err:
                raise temporalio.exceptions.ApplicationError(
                    "Failed decoding arguments", non_retryable=True
                ) from err

            # Convert heartbeat details
            # TODO(cretz): Allow some way to configure heartbeat type hinting?
            try:
                heartbeat_details = (
                    []
                    if not start.heartbeat_details
                    else await converter.decode(start.heartbeat_details)
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
                heartbeat_details=heartbeat_details,
                heartbeat_timeout=_proto_maybe_timedelta(start.heartbeat_timeout)
                if start.HasField("heartbeat_timeout")
                else None,
                schedule_to_close_timeout=_proto_maybe_timedelta(
                    start.schedule_to_close_timeout
                )
                if start.HasField("schedule_to_close_timeout")
                else None,
                scheduled_time=_proto_maybe_datetime(start.scheduled_time)
                if start.HasField("scheduled_time")
                else None,
                start_to_close_timeout=_proto_maybe_timedelta(
                    start.start_to_close_timeout
                )
                if start.HasField("start_to_close_timeout")
                else None,
                started_time=_proto_maybe_datetime(start.started_time)
                if start.HasField("started_time")
                else None,
                task_queue=self._config["task_queue"],
                task_token=task_token,
                workflow_id=start.workflow_execution.workflow_id,
                workflow_namespace=start.workflow_namespace,
                workflow_run_id=start.workflow_execution.run_id,
                workflow_type=start.workflow_type,
            )
            input = ExecuteActivityInput(
                fn=activity_def.fn,
                args=args,
                executor=None
                if inspect.iscoroutinefunction(activity_def.fn)
                else self._config["activity_executor"],
                _cancelled_event=running_activity.cancelled_event,
                _worker=self,
            )

            # Set the context early so the logging adapter works and
            # interceptors have it
            temporalio.activity._Context.set(
                temporalio.activity._Context(
                    info=lambda: info,
                    heartbeat=None,
                    cancelled_event=running_activity.cancelled_event,
                )
            )
            temporalio.activity.logger.debug("Starting activity")

            # Build the interceptors chaining in reverse. We build a context right
            # now even though the info() can't be intercepted and heartbeat() will
            # fail. The interceptors may want to use the info() during init.
            impl: ActivityInboundInterceptor = _ActivityInboundImpl()
            for interceptor in reversed(list(self._config["interceptors"])):
                impl = interceptor.intercept_activity(impl)
            # Init
            impl.init(_ActivityOutboundImpl(self, info))
            # Exec
            result = await impl.execute_activity(input)
            # Convert result if not none. Since Python essentially only supports
            # single result types (even if they are tuples), we will do the
            # same.
            if result is None:
                completion.result.completed.SetInParent()
            else:
                result_payloads = await self._config["client"].data_converter.encode(
                    [result]
                )
                # We have to convert from Temporal API payload to Core payload
                completion.result.completed.result.metadata.update(
                    result_payloads[0].metadata
                )
                completion.result.completed.result.data = result_payloads[0].data
        except (BaseException, asyncio.CancelledError) as err:
            try:
                if isinstance(err, temporalio.activity.CompleteAsyncError):
                    temporalio.activity.logger.debug("Completing asynchronously")
                    completion.result.will_complete_async.SetInParent()
                elif isinstance(err, asyncio.CancelledError):
                    temporalio.activity.logger.debug("Completing as cancelled")
                    # We intentionally have a separate activity CancelledError
                    # so we don't accidentally bubble a cancellation that
                    # happened for another reason
                    await temporalio.exceptions.apply_error_to_failure(
                        # TODO(cretz): Should use some other message?
                        temporalio.exceptions.CancelledError("Cancelled"),
                        self._config["client"].data_converter,
                        completion.result.cancelled.failure,
                    )
                else:
                    temporalio.activity.logger.debug(
                        f"Completing as failed with error of type {type(err)}: {err}",
                        extra={"error": err},
                        exc_info=True,
                    )
                    await temporalio.exceptions.apply_exception_to_failure(
                        err,
                        self._config["client"].data_converter,
                        completion.result.failed.failure,
                    )
            except Exception as inner_err:
                temporalio.activity.logger.exception(
                    f"Exception handling failed, original error: {err}, failure: {inner_err}",
                    extra={"error": err},
                )
                completion.result.Clear()
                completion.result.failed.failure.message = (
                    f"Failed building exception result: {inner_err}"
                )

        # Send task completion to core
        del self._running_activities[task_token]
        try:
            await self._bridge_worker.complete_activity_task(completion)
        except Exception as err:
            temporalio.activity.logger.exception(
                f"Failed completing activity task: {err}", extra={"error": err}
            )


class WorkerConfig(typing_extensions.TypedDict):
    """TypedDict of config originally passed to :py:meth:`Worker`."""

    client: temporalio.client.Client
    task_queue: str
    activities: Mapping[str, Callable]
    activity_executor: Optional[concurrent.futures.Executor]
    interceptors: Iterable[Interceptor]
    max_cached_workflows: int
    max_outstanding_workflow_tasks: int
    max_outstanding_activities: int
    max_outstanding_local_activities: int
    max_concurrent_wft_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_at_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout: timedelta
    max_heartbeat_throttle_interval: timedelta
    default_heartbeat_throttle_interval: timedelta
    shared_state_manager: Optional[SharedStateManager]
    type_hint_eval_str: bool


@dataclass
class _ActivityDefinition:
    name: str
    fn: Callable[..., Any]
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


@dataclass
class _RunningActivity:
    task: Optional[asyncio.Task]
    cancelled_event: threading.Event
    sync: bool

    def cancel(self) -> None:
        self.cancelled_event.set()
        # We do not cancel the task of sync activities
        if not self.sync and self.task:
            self.task.cancel()


@dataclass
class ExecuteActivityInput:
    fn: Callable[..., Any]
    args: Iterable[Any]
    executor: Optional[concurrent.futures.Executor]
    _cancelled_event: threading.Event
    _worker: Worker


class Interceptor:
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return next


class ActivityInboundInterceptor:
    def __init__(self, next: ActivityInboundInterceptor) -> None:
        self.next = next

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        self.next.init(outbound)

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        return await self.next.execute_activity(input)


class ActivityOutboundInterceptor:
    def __init__(self, next: ActivityOutboundInterceptor) -> None:
        self.next = next

    def info(self) -> temporalio.activity.Info:
        return self.next.info()

    def heartbeat(self, *details: Any) -> None:
        self.next.heartbeat(*details)


class _ActivityInboundImpl(ActivityInboundInterceptor):
    def __init__(self) -> None:
        # We are intentionally not calling the base class's __init__ here
        pass

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        # Set the context callables. We are setting values instead of replacing
        # the context just in case other interceptors held a reference.
        context = temporalio.activity._Context.current()
        context.info = outbound.info
        context.heartbeat = outbound.heartbeat

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        # Handle synchronous activity
        if not inspect.iscoroutinefunction(input.fn):
            # We execute a top-level function via the executor. It is top-level
            # because it needs to be picklable. Also, by default Python does not
            # propagate contexts into executor futures so we don't either with
            # the obvious exception of the info (if they want more, they can set
            # the initializer on the executor).
            ctx = temporalio.activity._Context.current()
            info = ctx.info()

            # Heartbeat calls internally use a data converter which is async so
            # they need to be called on the event loop
            loop = asyncio.get_running_loop()
            orig_heartbeat = ctx.heartbeat

            def thread_safe_heartbeat(*details: Any) -> None:
                temporalio.activity._Context.set(ctx)
                loop.call_soon_threadsafe(orig_heartbeat, *details)

            ctx.heartbeat = thread_safe_heartbeat

            # For heartbeats, we use the existing heartbeat callable for thread
            # pool executors or a multiprocessing queue for others
            heartbeat: Union[Callable[..., None], SharedHeartbeatSender] = ctx.heartbeat
            shared_manager: Optional[SharedStateManager] = None
            if not isinstance(input.executor, concurrent.futures.ThreadPoolExecutor):
                # Should always be present in worker, pre-checked on init
                shared_manager = input._worker._config["shared_state_manager"]
                assert shared_manager
                heartbeat = shared_manager.register_heartbeater(
                    info.task_token, ctx.heartbeat
                )

            try:
                return await asyncio.get_running_loop().run_in_executor(
                    input.executor,
                    _execute_sync_activity,
                    info,
                    heartbeat,
                    input._cancelled_event,
                    input.fn,
                    *input.args,
                )
            finally:
                if shared_manager:
                    shared_manager.unregister_heartbeater(info.task_token)

        # Otherwise for async activity, just run
        return await input.fn(*input.args)


# This has to be defined at the top-level to be picklable for process executors
def _execute_sync_activity(
    info: temporalio.activity.Info,
    heartbeat: Union[Callable[..., None], SharedHeartbeatSender],
    cancelled_event: threading.Event,
    fn: Callable[..., Any],
    *args: Any,
) -> Any:
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
            cancelled_event=cancelled_event,
        )
    )
    return fn(*args)


class _ActivityOutboundImpl(ActivityOutboundInterceptor):
    def __init__(self, worker: Worker, info: temporalio.activity.Info) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._info = info

    def info(self) -> temporalio.activity.Info:
        return self._info

    def heartbeat(self, *details: Any) -> None:
        info = temporalio.activity.info()
        self._worker._heartbeat_activity(info.task_token, *details)


def _proto_maybe_timedelta(
    dur: google.protobuf.duration_pb2.Duration,
) -> Optional[timedelta]:
    if dur.seconds == 0 and dur.nanos == 0:
        return None
    return dur.ToTimedelta()


def _proto_maybe_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> Optional[datetime]:
    if ts.seconds == 0 and ts.nanos == 0:
        return None
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


class SharedStateManager(ABC):
    # TODO(cretz): Document that queue should be a multiprocessing.Manager().Queue()
    # and that executor defaults as a single-worker thread pool executor
    @staticmethod
    def create_from_multiprocessing(
        mgr: multiprocessing.managers.SyncManager,
        queue_poller_executor: Optional[concurrent.futures.Executor] = None,
    ) -> SharedStateManager:
        return _MultiprocessingSharedStateManager(
            mgr, queue_poller_executor or concurrent.futures.ThreadPoolExecutor(1)
        )

    @abstractmethod
    def new_cancellation_event(self) -> threading.Event:
        raise NotImplementedError

    @abstractmethod
    def register_heartbeater(
        self, task_token: bytes, heartbeat: Callable[..., None]
    ) -> SharedHeartbeatSender:
        raise NotImplementedError

    @abstractmethod
    def unregister_heartbeater(self, task_token: bytes) -> None:
        raise NotImplementedError


class SharedHeartbeatSender(ABC):
    @abstractmethod
    def send_heartbeat(self, task_token: bytes, *details: Any) -> None:
        raise NotImplementedError


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
        self._heartbeat_queue: queue.Queue[Tuple[bytes, Iterable[Any]]] = mgr.Queue(
            1000
        )
        self._heartbeats: Dict[bytes, Callable[..., None]] = {}

    def new_cancellation_event(self) -> threading.Event:
        return self._mgr.Event()

    def register_heartbeater(
        self, task_token: bytes, heartbeat: Callable[..., None]
    ) -> SharedHeartbeatSender:
        self._heartbeats[task_token] = heartbeat
        # If just now non-empty, start processor
        if len(self._heartbeats) == 1:
            self._queue_poller_executor.submit(self._heartbeat_processor)
        return _MultiprocessingSharedHeartbeatSender(self._heartbeat_queue)

    def unregister_heartbeater(self, task_token: bytes) -> None:
        del self._heartbeats[task_token]

    def _heartbeat_processor(self) -> None:
        while len(self._heartbeats) > 0:
            try:
                # The timeout here of 0.5 seconds is how long until we try
                # again. This timeout then is the max amount of time before this
                # processor can stop when there are no more activity heartbeats
                # registered.
                # TODO(cretz): Need to be configurable or derived from heartbeat
                # timeouts on activities themselves? E.g. 0.8 of the current
                # shortest registered timeout? It wouldn't really add much
                # benefit except for stopping speed
                item: Tuple[bytes, Iterable[Any]] = self._heartbeat_queue.get(True, 0.5)
                # We count on this being a _very_ cheap function
                fn = self._heartbeats.get(item[0])
                if fn:
                    fn(*item[1])
            except queue.Empty:
                pass
            except Exception as err:
                logger.exception(
                    "Failed during multiprocess queue poll for heartbeat: {err}",
                    extra={"error": err},
                )
                return


class _MultiprocessingSharedHeartbeatSender(SharedHeartbeatSender):
    def __init__(
        self, heartbeat_queue: queue.Queue[Tuple[bytes, Iterable[Any]]]
    ) -> None:
        super().__init__()
        self._heartbeat_queue = heartbeat_queue

    def send_heartbeat(self, task_token: bytes, *details: Any) -> None:
        # No wait
        self._heartbeat_queue.put((task_token, details), False)
