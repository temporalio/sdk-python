from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import itertools
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

import typing_extensions

import temporalio.activity
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.activity_task
import temporalio.bridge.worker
import temporalio.client
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
        async_activity_executor: Optional[concurrent.futures.Executor] = None,
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
            if not inspect.isgeneratorfunction(activity) and not activity_executor:
                raise ValueError(
                    f"Activity {name} is not async so an activity_executor must be present"
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
            async_activity_executor=async_activity_executor,
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
        self._started = False

    def config(self) -> WorkerConfig:
        """Config, as a dictionary, used to create this worker.

        This makes a shallow copy of the config each call.
        """
        config = self._config.copy()
        config["activities"] = dict(config["activities"])
        return config

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("Run already called")
        self._started = True
        await asyncio.wait([self._run_activities()])

    async def shutdown(self) -> None:
        await self._bridge_worker.shutdown()

    async def _run_activities(self) -> None:
        # Continually poll for activity work
        while True:
            try:
                # Poll for a task
                task = await self._bridge_worker.poll_activity_task()

                if task.HasField("start"):
                    try:
                        await self._start_activity(task.start)
                    except Exception as err:
                        logger.warning(
                            "Failed starting activity %s due to %s",
                            task.start.activity_type,
                            err,
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
        self, start: temporalio.bridge.proto.activity_task.Start
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
        # TODO(cretz): Support passing type-hints for help in converting
        converter = self._config["client"].data_converter
        args = await converter.decode()
        raise NotImplementedError

    def _cancel_activity(
        self, cancel: temporalio.bridge.proto.activity_task.Start
    ) -> None:
        raise NotImplementedError


class WorkerConfig(typing_extensions.TypedDict):
    """TypedDict of config originally passed to :py:meth:`Worker`."""

    client: temporalio.client.Client
    task_queue: str
    activities: Mapping[str, Callable]
    activity_executor: Optional[concurrent.futures.Executor]
    async_activity_executor: Optional[concurrent.futures.Executor]
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


class _Activity:
    pass


@dataclass
class ExecuteActivityInput:
    args: Iterable[Any]


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

    def execute_activity(self, input: ExecuteActivityInput) -> Any:
        return self.next.execute_activity(input)


class ActivityOutboundInterceptor:
    def __init__(self, next: ActivityOutboundInterceptor) -> None:
        self.next = next

    def info(self) -> temporalio.activity.Info:
        return self.next.info()

    def heartbeat(self, *details: Any) -> None:
        self.next.heartbeat(*details)
