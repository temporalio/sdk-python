"""Worker for processing Temporal workflows and/or activities."""

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
import temporalio.api.common.v1
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
    """Worker to process workflows and/or activities.

    Once created, workers can be run and shutdown explicitly via :py:meth:`run`
    and :py:meth:`shutdown`, or they can be used in an ``async with`` clause.
    """

    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        task_queue: str,
        activities: Mapping[str, Callable] = {},
        activity_executor: Optional[concurrent.futures.Executor] = None,
        interceptors: Iterable[Interceptor] = [],
        max_cached_workflows: int = 0,
        max_concurrent_workflow_tasks: int = 100,
        max_concurrent_activities: int = 100,
        max_concurrent_local_activities: int = 100,
        max_concurrent_wft_polls: int = 5,
        nonsticky_to_sticky_poll_ratio: float = 0.2,
        max_concurrent_at_polls: int = 5,
        no_remote_activities: bool = False,
        sticky_queue_schedule_to_start_timeout: timedelta = timedelta(seconds=10),
        max_heartbeat_throttle_interval: timedelta = timedelta(seconds=60),
        default_heartbeat_throttle_interval: timedelta = timedelta(seconds=30),
        graceful_shutdown_timeout: timedelta = timedelta(),
        shared_state_manager: Optional[SharedStateManager] = None,
    ) -> None:
        """Create a worker to process workflows and/or activities.

        Args:
            client: Client to use for this worker. This is required and must be
                the :py:class:`temporalio.client.Client` instance or have a
                worker_workflow_service attribute with reference to the original
                client's underlying service.
            task_queue: Required task queue for this worker.
            activities: Mapping of activity type names to activity callables.
                Activities may be async functions or non-async functions.
            activity_executor: Concurrent executor to use for non-async
                activities. This is required if any activities are non-async. If
                this is a :py:class:`concurrent.futures.ProcessPoolExecutor`,
                all non-async activities must be picklable.
            interceptors: Collection of interceptors for this worker. Any
                interceptors already on the client that also implement
                :py:class:`Interceptor` are prepended to this list and should
                not be explicitly given here.
            max_cached_workflows: If nonzero, workflows will be cached and
                sticky task queues will be used.
            max_concurrent_workflow_tasks: Maximum allowed number of workflow
                tasks that will ever be given to this worker at one time.
            max_concurrent_activities: Maximum number of activity tasks that
                will ever be given to this worker concurrently.
            max_concurrent_local_activities: Maximum number of local activity
                tasks that will ever be given to this worker concurrently.
            max_concurrent_wft_polls: Maximum number of concurrent poll workflow
                task requests we will perform at a time on this worker's task
                queue.
            nonsticky_to_sticky_poll_ratio: max_concurrent_wft_polls * this
                number = the number of max pollers that will be allowed for the
                nonsticky queue when sticky tasks are enabled. If both defaults
                are used, the sticky queue will allow 4 max pollers while the
                nonsticky queue will allow one. The minimum for either poller is
                1, so if ``max_concurrent_wft_polls`` is 1 and sticky queues are
                enabled, there will be 2 concurrent polls.
            max_concurrent_at_polls: Maximum number of concurrent poll activity
                task requests we will perform at a time on this worker's task
                queue.
            no_remote_activities: If true, this worker will only handle workflow
                tasks and local activities, it will not poll for activity tasks.
            sticky_queue_schedule_to_start_timeout: How long a workflow task is
                allowed to sit on the sticky queue before it is timed out and
                moved to the non-sticky queue where it may be picked up by any
                worker.
            max_heartbeat_throttle_interval: Longest interval for throttling
                activity heartbeats.
            default_heartbeat_throttle_interval: Default interval for throttling
                activity heartbeats in case per-activity heartbeat timeout is
                unset. Otherwise, it's the per-activity heartbeat timeout * 0.8.
            graceful_shutdown_timeout: Amount of time after shutdown is called
                that activities are given to complete before their tasks are
                cancelled.
            shared_state_manager: Used for obtaining cross-process friendly
                synchronization primitives. This is required for non-async
                activities where the activity_executor is not a
                :py:class:`concurrent.futures.ThreadPoolExecutor`. Reuse of
                these across workers is encouraged.
        """
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
                activity, eval_str=client._config["type_hint_eval_str"]
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
                max_outstanding_workflow_tasks=max_concurrent_workflow_tasks,
                max_outstanding_activities=max_concurrent_activities,
                max_outstanding_local_activities=max_concurrent_local_activities,
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
            max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
            max_concurrent_activities=max_concurrent_activities,
            max_concurrent_local_activities=max_concurrent_local_activities,
            max_concurrent_wft_polls=max_concurrent_wft_polls,
            nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
            max_concurrent_at_polls=max_concurrent_at_polls,
            no_remote_activities=no_remote_activities,
            sticky_queue_schedule_to_start_timeout=sticky_queue_schedule_to_start_timeout,
            max_heartbeat_throttle_interval=max_heartbeat_throttle_interval,
            default_heartbeat_throttle_interval=default_heartbeat_throttle_interval,
            graceful_shutdown_timeout=graceful_shutdown_timeout,
            shared_state_manager=shared_state_manager,
        )
        self._running_activities: Dict[bytes, _RunningActivity] = {}
        self._task: Optional[asyncio.Task] = None

    def config(self) -> WorkerConfig:
        """Config, as a dictionary, used to create this worker.

        Returns:
            Configuration, shallow-copied.
        """
        config = self._config.copy()
        config["activities"] = dict(config["activities"])
        return config

    @property
    def task_queue(self) -> str:
        """Task queue this worker is on."""
        return self._config["task_queue"]

    async def __aenter__(self) -> Worker:
        """Start the worker and return self for use by ``async with``.

        Returns:
            Self.
        """
        self._start()
        return self

    async def __aexit__(self, *args) -> None:
        """Same as :py:meth:`shutdown` for use by ``async with``."""
        await self.shutdown()

    async def run(self) -> None:
        """Run the worker and wait on it to be shutdown."""
        await self._start()

    def _start(self) -> asyncio.Task:
        if self._task:
            raise RuntimeError("Already started")
        self._task = asyncio.create_task(
            asyncio.wait([asyncio.create_task(self._run_activities())])
        )
        return self._task

    async def shutdown(self) -> None:
        """Shutdown the worker and wait until all activities have completed.

        This will initiate a shutdown and optionally wait for a grace period
        before sending cancels to all activities.
        """
        if not self._task:
            raise RuntimeError("Never started")
        graceful_timeout = self._config["graceful_shutdown_timeout"]
        logger.info(
            f"Beginning worker shutdown, will wait {graceful_timeout} before cancelling workflows/activities"
        )
        # Start shutdown of the bridge
        bridge_shutdown_task = asyncio.create_task(self._bridge_worker.shutdown())
        # Wait for the poller loop to stop
        await self._task

        # Collect all activity tasks while telling them we're shutting down
        activity_tasks: List[asyncio.Task] = []
        for activity in self._running_activities.values():
            if activity.task:
                activity_tasks.append(activity.task)
                activity.worker_shutdown_event.set()
        if activity_tasks:
            # Wait for any still running after graceful timeout
            _, still_running = await asyncio.wait(
                activity_tasks, timeout=graceful_timeout.total_seconds()
            )
            if still_running:
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
                        if activity.task is task:
                            activity.cancel()
                            break

        # Wait for the bridge to report all activities are completed
        await bridge_shutdown_task

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
                        task=None,
                        cancelled_event=temporalio.activity._CompositeEvent(
                            thread_event=threading.Event(), async_event=None
                        ),
                        worker_shutdown_event=temporalio.activity._CompositeEvent(
                            thread_event=threading.Event(), async_event=None
                        ),
                        sync=False,
                        last_heartbeat_task=None,
                    )
                    activity.task = asyncio.create_task(
                        self._run_activity(task.task_token, task.start, activity)
                    )
                    self._running_activities[task.task_token] = activity
                elif task.HasField("cancel"):
                    self._cancel_activity(task.task_token, task.cancel)
                else:
                    raise RuntimeError(f"Unrecognized activity task: {task}")
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
        activity.cancel(cancelled_by_request=True)

    def _heartbeat_activity(self, task_token: bytes, *details: Any) -> None:
        # We intentionally make heartbeating non-async, but since the data
        # converter is async, we have to schedule it
        logger = temporalio.activity.logger
        activity = self._running_activities.get(task_token)
        if activity:
            activity.current_heartbeat_seq += 1
            heartbeat_seq = activity.current_heartbeat_seq
            activity.last_heartbeat_task = asyncio.create_task(
                self._heartbeat_activity_async(
                    logger, task_token, heartbeat_seq, *details
                )
            )

    async def _heartbeat_activity_async(
        self,
        logger: logging.Logger,
        task_token: bytes,
        heartbeat_seq: int,
        *details: Any,
    ) -> None:
        activity = self._running_activities.get(task_token)
        # Bail if not the latest
        if not activity or activity.current_heartbeat_seq != heartbeat_seq:
            return
        try:
            heartbeat = temporalio.bridge.proto.ActivityHeartbeat(task_token=task_token)
            if details:
                converted_details = await self._config["client"].data_converter.encode(
                    details
                )
                # Convert to core payloads
                heartbeat.details.extend(
                    [
                        temporalio.bridge.proto.common.Payload(
                            metadata=p.metadata, data=p.data
                        )
                        for p in converted_details
                    ]
                )
            # Bail if not the latest
            if activity.current_heartbeat_seq != heartbeat_seq:
                return
            self._bridge_worker.record_activity_heartbeat(heartbeat)
        except Exception as err:
            # Bail if not the latest
            if activity.current_heartbeat_seq != heartbeat_seq:
                return
            # If the activity is done, nothing we can do but log
            if activity.done:
                logger.exception(
                    f"Failed recording heartbeat (activity already done, cannot error): {err}",
                    extra={"error": err},
                )
            else:
                logger.warning(
                    f"Cancelling activity because failed recording heartbeat: {err}",
                    extra={"error": err},
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
            else:
                # We have to set the async form of events
                running_activity.cancelled_event.async_event = asyncio.Event()
                running_activity.worker_shutdown_event.async_event = asyncio.Event()

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
                # Use cross-process events
                running_activity.cancelled_event.thread_event = manager.new_event()
                running_activity.worker_shutdown_event.thread_event = (
                    manager.new_event()
                )

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
                current_attempt_scheduled_time=_proto_to_datetime(
                    start.current_attempt_scheduled_time
                )
                if start.HasField("current_attempt_scheduled_time")
                else None,
                header={
                    k: temporalio.api.common.v1.Payload(
                        metadata=v.metadata, data=v.data
                    )
                    for k, v in start.header_fields.items()
                },
                heartbeat_details=heartbeat_details,
                heartbeat_timeout=start.heartbeat_timeout.ToTimedelta()
                if start.HasField("heartbeat_timeout")
                else None,
                is_local=False,
                retry_policy=temporalio.bridge.worker.retry_policy_from_proto(
                    start.retry_policy
                )
                if start.HasField("retry_policy")
                else None,
                schedule_to_close_timeout=start.schedule_to_close_timeout.ToTimedelta()
                if start.HasField("schedule_to_close_timeout")
                else None,
                scheduled_time=_proto_to_datetime(start.scheduled_time)
                if start.HasField("scheduled_time")
                else None,
                start_to_close_timeout=start.start_to_close_timeout.ToTimedelta()
                if start.HasField("start_to_close_timeout")
                else None,
                started_time=_proto_to_datetime(start.started_time)
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
                _worker_shutdown_event=running_activity.worker_shutdown_event,
                _worker=self,
            )

            # Set the context early so the logging adapter works and
            # interceptors have it
            temporalio.activity._Context.set(
                temporalio.activity._Context(
                    info=lambda: info,
                    heartbeat=None,
                    cancelled_event=running_activity.cancelled_event,
                    worker_shutdown_event=running_activity.worker_shutdown_event,
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
        except (Exception, asyncio.CancelledError) as err:
            try:
                if isinstance(err, temporalio.activity.CompleteAsyncError):
                    temporalio.activity.logger.debug("Completing asynchronously")
                    completion.result.will_complete_async.SetInParent()
                elif (
                    isinstance(err, asyncio.CancelledError)
                    and running_activity.cancelled_due_to_heartbeat_error
                ):
                    err = running_activity.cancelled_due_to_heartbeat_error
                    temporalio.activity.logger.debug(
                        f"Completing as failure during heartbeat with error of type {type(err)}: {err}",
                        extra={"error": err},
                        exc_info=True,
                    )
                    await temporalio.exceptions.apply_exception_to_failure(
                        err,
                        self._config["client"].data_converter,
                        completion.result.failed.failure,
                    )
                elif (
                    isinstance(err, asyncio.CancelledError)
                    and running_activity.cancelled_by_request
                ):
                    temporalio.activity.logger.debug("Completing as cancelled")
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

        # If there was a last heartbeat task, we have to wait on it. We mark the
        # activity as already done to make sure that the heartbeater doesn't try
        # to cancel it if it fails.
        running_activity.done = True
        if running_activity.last_heartbeat_task:
            task = running_activity.last_heartbeat_task
            running_activity.last_heartbeat_task = None
            try:
                await task
            except:
                # Should never happen because it's trapped in-task
                temporalio.activity.logger.exception(
                    "Final heartbeat task didn't trap error"
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
    """TypedDict of config originally passed to :py:class:`Worker`."""

    client: temporalio.client.Client
    task_queue: str
    activities: Mapping[str, Callable]
    activity_executor: Optional[concurrent.futures.Executor]
    interceptors: Iterable[Interceptor]
    max_cached_workflows: int
    max_concurrent_workflow_tasks: int
    max_concurrent_activities: int
    max_concurrent_local_activities: int
    max_concurrent_wft_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_at_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout: timedelta
    max_heartbeat_throttle_interval: timedelta
    default_heartbeat_throttle_interval: timedelta
    graceful_shutdown_timeout: timedelta
    shared_state_manager: Optional[SharedStateManager]


@dataclass
class _ActivityDefinition:
    name: str
    fn: Callable[..., Any]
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


@dataclass
class _RunningActivity:
    task: Optional[asyncio.Task]
    cancelled_event: temporalio.activity._CompositeEvent
    worker_shutdown_event: temporalio.activity._CompositeEvent
    last_heartbeat_task: Optional[asyncio.Task]
    sync: bool
    done: bool = False
    # Increased each heartbeat, heartbeats not at this seq are ignored. The
    # reason for this is to ensure that if async heartbeats appear out of order
    # (which they can due to variable data converter speeds), only the latest
    # one is applied.
    current_heartbeat_seq: int = 1
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
        self.cancelled_event.set()
        # We do not cancel the task of sync activities
        if not self.sync and self.task:
            # TODO(cretz): Check that Python >= 3.9 and set msg?
            self.task.cancel()


@dataclass
class ExecuteActivityInput:
    """Input for :py:meth:`ActivityInboundInterceptor.execute_activity`."""

    fn: Callable[..., Any]
    args: Iterable[Any]
    executor: Optional[concurrent.futures.Executor]
    _cancelled_event: temporalio.activity._CompositeEvent
    _worker_shutdown_event: temporalio.activity._CompositeEvent
    _worker: Worker


class Interceptor:
    """Interceptor for workers.

    This should be extended by any worker interceptors.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Method called for intercepting an activity.

        Args:
            next: The underlying inbound interceptor this interceptor should
                delegate to.

        Returns:
            The new interceptor that will be used to for the activity.
        """
        return next


class ActivityInboundInterceptor:
    """Inbound interceptor to wrap outbound creation and activity execution.

    This should be extended by any activity inbound interceptors.
    """

    def __init__(self, next: ActivityInboundInterceptor) -> None:
        """Create the inbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        """Initialize with an outbound interceptor.

        To add a custom outbound interceptor, wrap the given interceptor before
        sending to the next ``init`` call.
        """
        self.next.init(outbound)

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Called to invoke the activity."""
        return await self.next.execute_activity(input)


class ActivityOutboundInterceptor:
    """Outbound interceptor to wrap calls made from within activities.

    This should be extended by any activity outbound interceptors.
    """

    def __init__(self, next: ActivityOutboundInterceptor) -> None:
        """Create the outbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def info(self) -> temporalio.activity.Info:
        """Called for every :py:func:`temporalio.activity.info` call."""
        return self.next.info()

    def heartbeat(self, *details: Any) -> None:
        """Called for every :py:func:`temporalio.activity.heartbeat` call."""
        self.next.heartbeat(*details)

    # TODO(cretz): Do we want outbound interceptors for other items?


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
            # propagate contextvars into executor futures so we don't either
            # with the obvious exception of our context (if they want more, they
            # can set the initializer on the executor).
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
                    # Only thread event, this may cross a process boundary
                    input._cancelled_event.thread_event,
                    input._worker_shutdown_event.thread_event,
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
    worker_shutdown_event: threading.Event,
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
            cancelled_event=temporalio.activity._CompositeEvent(
                thread_event=cancelled_event, async_event=None
            ),
            worker_shutdown_event=temporalio.activity._CompositeEvent(
                thread_event=worker_shutdown_event, async_event=None
            ),
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


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> Optional[datetime]:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


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
    def register_heartbeater(
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
    def unregister_heartbeater(self, task_token: bytes) -> None:
        """Unregisters a previously registered heartbeater for the task
        token.
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

    def new_event(self) -> threading.Event:
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