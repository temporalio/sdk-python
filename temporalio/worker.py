from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import itertools
import logging
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

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
    ) -> None:
        # TODO(cretz): Support workflows
        if not activities:
            raise ValueError("At least one activity must be specified")
        # If there are any non-async activities, an executor is required
        for name, activity in activities.items():
            if not callable(activity):
                raise TypeError(f"Activity {name} is not callable")
            if not inspect.iscoroutinefunction(activity):
                if not activity_executor:
                    raise ValueError(
                        f"Activity {name} is not async so an activity_executor must be present"
                    )
                elif isinstance(
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
        )
        self._running_activities: Dict[bytes, asyncio.Future] = {}
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
                    try:
                        await self._start_activity(task.task_token, task.start)
                    except Exception as err:
                        logger.warning(
                            "Failed starting activity %s due to %s",
                            task.start.activity_type,
                            err,
                            exc_info=True,
                        )
                        # If failed to start, report failure
                        await self._bridge_worker.complete_activity_task(
                            temporalio.bridge.proto.ActivityTaskCompletion(
                                task_token=task.task_token,
                                result=temporalio.bridge.proto.activity_result.ActivityExecutionResult(
                                    failed=temporalio.exceptions.exception_to_failure(
                                        err,
                                        self._config["client"].data_converter,
                                    ),
                                ),
                            )
                        )
                elif task.HasField("cancel"):
                    self._cancel_activity(task.cancel)
                else:
                    logger.warning("unrecognized activity task: %s", task)
            except temporalio.bridge.worker.PollShutdownError:
                return

    async def _run_workflows(self) -> None:
        raise NotImplementedError

    async def _start_activity(
        self, task_token: bytes, start: temporalio.bridge.proto.activity_task.Start
    ) -> None:
        # Find activity or fail
        fn = self._config["activities"].get(start.activity_type)
        if not fn:
            activity_names = ", ".join(sorted(self._config["activities"].keys()))
            raise temporalio.exceptions.ApplicationError(
                f"Activity function {start.activity_type} is not registered on this worker, available activities: {activity_names}",
                type="NotFoundError",
                non_retryable=True,
            )

        # Convert arguments
        arg_types, ret_type = await temporalio.converter.type_hints_from_func(
            fn, require_arg_count=len(start.input)
        )
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
                [] if not start.input else converter.decode(start.heartbeat_details)
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
            start_to_close_timeout=_proto_maybe_timedelta(start.start_to_close_timeout)
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
            fn=fn,
            args=args,
            executor=None
            if inspect.iscoroutinefunction(fn)
            else self._config["activity_executor"],
        )

        self._running_activities[task_token] = asyncio.ensure_future(
            self._run_activity(info, input)
        )

    def _cancel_activity(
        self, cancel: temporalio.bridge.proto.activity_task.Start
    ) -> None:
        raise NotImplementedError

    def _heartbeat_activity(self, task_token: bytes, *details: Any) -> None:
        raise NotImplementedError

    async def _run_activity(
        self, info: temporalio.activity.Info, input: ExecuteActivityInput
    ) -> None:
        # We choose to surround interceptor creation and activity invocation in
        # a try block so we can mark the workflow as failed on any error instead
        # of having error handling in the interceptor
        completion = temporalio.bridge.proto.ActivityTaskCompletion(
            task_token=info.task_token
        )
        try:
            # Set the context early so the logging adapter works and
            # interceptors have it
            temporalio.activity._Context.set(
                temporalio.activity._Context(info=lambda: info, heartbeat=None)
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
        except temporalio.activity.CompleteAsyncError:
            temporalio.activity.logger.debug("Completing asynchronously")
            completion.result.will_complete_async.SetInParent()
        except temporalio.activity.CancelledError:
            temporalio.activity.logger.debug("Completing as cancelled")
            # We intentionally have a separate activity CancelledError so we
            # don't accidentally bubble a cancellation that happened for another
            # reason
            await temporalio.exceptions.apply_error_to_failure(
                # TODO(cretz): Should use some other message?
                temporalio.exceptions.CancelledError("Cancelled"),
                self._config["client"].data_converter,
                completion.result.cancelled.failure,
            )
        except Exception as err:
            temporalio.activity.logger.debug(
                f"Completing as failed with error: {err}", extra={"error": err}
            )
            await temporalio.exceptions.apply_exception_to_failure(
                err,
                self._config["client"].data_converter,
                completion.result.cancelled.failure,
            )

        # Send task completion to core
        await self._bridge_worker.complete_activity_task(completion)


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


@dataclass
class ExecuteActivityInput:
    fn: Callable[..., Any]
    args: Iterable[Any]
    executor: Optional[concurrent.futures.Executor]


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
        # Await asyncs, otherwise just run
        if not inspect.iscoroutinefunction(input.fn):
            # We execute a top-level function via the executor. It is top-level
            # because it needs to be picklable. Also, by default Python does not
            # propagate contexts into executor futures so we don't either with
            # the obvious exception of the info (if they want more, they can set
            # the initializer on the executor).
            # TODO(cretz): multiprocessing.Pipe-based heartbeat/cancel
            # We always expect an executor
            return await asyncio.get_running_loop().run_in_executor(
                input.executor,
                _execute_sync_activity,
                temporalio.activity.info(),
                input.fn,
                *input.args,
            )
        return await input.fn(*input.args)


# This has to be defined at the top-level to be picklable for process executors
def _execute_sync_activity(
    info: temporalio.activity.Info, fn: Callable[..., Any], *args: Any
) -> Any:
    temporalio.activity._Context.set(
        temporalio.activity._Context(
            info=lambda: info,
            # TODO(cretz): Pipe-based heartbeater, canceller, etc
            heartbeat=None,
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
