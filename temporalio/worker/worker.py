"""Worker for processing Temporal workflows and/or activities."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import sys
from datetime import timedelta
from typing import Any, Callable, List, Optional, Sequence, Type, cast

from typing_extensions import TypedDict

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.common
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.service

from .activity import SharedStateManager, _ActivityWorker
from .interceptor import Interceptor
from .workflow import _WorkflowWorker
from .workflow_instance import UnsandboxedWorkflowRunner, WorkflowRunner

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
        activities: Sequence[Callable] = [],
        workflows: Sequence[Type] = [],
        activity_executor: Optional[concurrent.futures.Executor] = None,
        workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None,
        workflow_runner: WorkflowRunner = UnsandboxedWorkflowRunner(),
        interceptors: Sequence[Interceptor] = [],
        build_id: Optional[str] = None,
        identity: Optional[str] = None,
        max_cached_workflows: int = 1000,
        max_concurrent_workflow_tasks: int = 100,
        max_concurrent_activities: int = 100,
        max_concurrent_local_activities: int = 100,
        max_concurrent_workflow_task_polls: int = 5,
        nonsticky_to_sticky_poll_ratio: float = 0.2,
        max_concurrent_activity_task_polls: int = 5,
        no_remote_activities: bool = False,
        sticky_queue_schedule_to_start_timeout: timedelta = timedelta(seconds=10),
        max_heartbeat_throttle_interval: timedelta = timedelta(seconds=60),
        default_heartbeat_throttle_interval: timedelta = timedelta(seconds=30),
        max_activities_per_second: Optional[float] = None,
        max_task_queue_activities_per_second: Optional[float] = None,
        graceful_shutdown_timeout: timedelta = timedelta(),
        shared_state_manager: Optional[SharedStateManager] = None,
        debug_mode: bool = False,
    ) -> None:
        """Create a worker to process workflows and/or activities.

        Args:
            client: Client to use for this worker. This is required and must be
                the :py:class:`temporalio.client.Client` instance or have a
                worker_service_client attribute with reference to the original
                client's underlying service client. This client cannot be
                "lazy".
            task_queue: Required task queue for this worker.
            activities: Set of activity callables decorated with
                :py:func:`@activity.defn<temporalio.activity.defn>`. Activities
                may be async functions or non-async functions.
            workflows: Set of workflow classes decorated with
                :py:func:`@workflow.defn<temporalio.workflow.defn>`.
            activity_executor: Concurrent executor to use for non-async
                activities. This is required if any activities are non-async. If
                this is a :py:class:`concurrent.futures.ProcessPoolExecutor`,
                all non-async activities must be picklable.
            workflow_task_executor: Thread pool executor for workflow tasks. If
                this is not present, a new
                :py:class:`concurrent.futures.ThreadPoolExecutor` will be
                created with ``max_workers`` set to ``max(os.cpu_count(), 4)``.
                The default one will be properly shutdown, but if one is
                provided, the caller is responsible for shutting it down after
                the worker is shut down.
            workflow_runner: Runner for workflows.
            interceptors: Collection of interceptors for this worker. Any
                interceptors already on the client that also implement
                :py:class:`Interceptor` are prepended to this list and should
                not be explicitly given here.
            build_id: Unique identifier for the current runtime. This is best
                set as a hash of all code and should change only when code does.
                If unset, a best-effort identifier is generated.
            identity: Identity for this worker client. If unset, the client
                identity is used.
            max_cached_workflows: If nonzero, workflows will be cached and
                sticky task queues will be used.
            max_concurrent_workflow_tasks: Maximum allowed number of workflow
                tasks that will ever be given to this worker at one time.
            max_concurrent_activities: Maximum number of activity tasks that
                will ever be given to this worker concurrently.
            max_concurrent_local_activities: Maximum number of local activity
                tasks that will ever be given to this worker concurrently.
            max_concurrent_workflow_task_polls: Maximum number of concurrent
                poll workflow task requests we will perform at a time on this
                worker's task queue.
            nonsticky_to_sticky_poll_ratio: max_concurrent_workflow_task_polls *
                this number = the number of max pollers that will be allowed for
                the nonsticky queue when sticky tasks are enabled. If both
                defaults are used, the sticky queue will allow 4 max pollers
                while the nonsticky queue will allow one. The minimum for either
                poller is 1, so if ``max_concurrent_workflow_task_polls`` is 1
                and sticky queues are enabled, there will be 2 concurrent polls.
            max_concurrent_activity_task_polls: Maximum number of concurrent
                poll activity task requests we will perform at a time on this
                worker's task queue.
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
            max_activities_per_second: Limits the number of activities per
                second that this worker will process. The worker will not poll
                for new activities if by doing so it might receive and execute
                an activity which would cause it to exceed this limit.
            max_task_queue_activities_per_second: Sets the maximum number of
                activities per second the task queue will dispatch, controlled
                server-side. Note that this only takes effect upon an activity
                poll request. If multiple workers on the same queue have
                different values set, they will thrash with the last poller
                winning.
            graceful_shutdown_timeout: Amount of time after shutdown is called
                that activities are given to complete before their tasks are
                cancelled.
            shared_state_manager: Used for obtaining cross-process friendly
                synchronization primitives. This is required for non-async
                activities where the activity_executor is not a
                :py:class:`concurrent.futures.ThreadPoolExecutor`. Reuse of
                these across workers is encouraged.
            debug_mode: If true, will disable deadlock detection and may disable
                sandboxing in order to make using a debugger easier. If false
                but the environment variable ``TEMPORAL_DEBUG`` is truthy, this
                will be set to true.
        """
        if not activities and not workflows:
            raise ValueError("At least one activity or workflow must be specified")

        # Prepend applicable client interceptors to the given ones
        client_config = client.config()
        interceptors_from_client = cast(
            List[Interceptor],
            [i for i in client_config["interceptors"] if isinstance(i, Interceptor)],
        )
        interceptors = interceptors_from_client + list(interceptors)

        # Instead of using the _type_lookup on the client, we create a separate
        # one here so we can continue to only use the public API of the client
        type_lookup = temporalio.converter._FunctionTypeLookup()

        # Extract the bridge service client. We try the service on the client
        # first, then we support a worker_service_client on the client's service
        # to return underlying service client we can use.
        bridge_client: temporalio.service._BridgeServiceClient
        if isinstance(client.service_client, temporalio.service._BridgeServiceClient):
            bridge_client = client.service_client
        elif hasattr(client.service_client, "worker_service_client"):
            bridge_client = client.service_client.worker_service_client
            if not isinstance(bridge_client, temporalio.service._BridgeServiceClient):
                raise TypeError(
                    "Client's worker_service_client cannot be used for a worker"
                )
        else:
            raise TypeError(
                "Client cannot be used for a worker. "
                + "Use the original client's service or set worker_service_client on the wrapped service with the original service client."
            )

        # Store the config for tracking
        self._config = WorkerConfig(
            client=client,
            task_queue=task_queue,
            activities=activities,
            workflows=workflows,
            activity_executor=activity_executor,
            workflow_task_executor=workflow_task_executor,
            workflow_runner=workflow_runner,
            interceptors=interceptors,
            build_id=build_id,
            identity=identity,
            max_cached_workflows=max_cached_workflows,
            max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
            max_concurrent_activities=max_concurrent_activities,
            max_concurrent_local_activities=max_concurrent_local_activities,
            max_concurrent_workflow_task_polls=max_concurrent_workflow_task_polls,
            nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
            max_concurrent_activity_task_polls=max_concurrent_activity_task_polls,
            no_remote_activities=no_remote_activities,
            sticky_queue_schedule_to_start_timeout=sticky_queue_schedule_to_start_timeout,
            max_heartbeat_throttle_interval=max_heartbeat_throttle_interval,
            default_heartbeat_throttle_interval=default_heartbeat_throttle_interval,
            max_activities_per_second=max_activities_per_second,
            max_task_queue_activities_per_second=max_task_queue_activities_per_second,
            graceful_shutdown_timeout=graceful_shutdown_timeout,
            shared_state_manager=shared_state_manager,
            debug_mode=debug_mode,
        )
        self._task: Optional[asyncio.Task] = None

        # Create activity and workflow worker
        self._activity_worker: Optional[_ActivityWorker] = None
        if activities:
            self._activity_worker = _ActivityWorker(
                bridge_worker=lambda: self._bridge_worker,
                task_queue=task_queue,
                activities=activities,
                activity_executor=activity_executor,
                shared_state_manager=shared_state_manager,
                data_converter=client_config["data_converter"],
                interceptors=interceptors,
                type_lookup=type_lookup,
            )
        self._workflow_worker: Optional[_WorkflowWorker] = None
        if workflows:
            self._workflow_worker = _WorkflowWorker(
                bridge_worker=lambda: self._bridge_worker,
                namespace=client.namespace,
                task_queue=task_queue,
                workflows=workflows,
                workflow_task_executor=workflow_task_executor,
                workflow_runner=workflow_runner,
                data_converter=client_config["data_converter"],
                interceptors=interceptors,
                debug_mode=debug_mode,
            )

        # We need an already connected client
        # TODO(cretz): How to connect to client inside constructor here? In the
        # meantime, we disallow lazy clients from being used for workers. We
        # could check whether the connected client is present which means
        # lazy-but-already-connected clients would work, but that is confusing
        # to users that the client only works if they already made a call on it.
        if bridge_client.config.lazy:
            raise RuntimeError("Lazy clients cannot be used for workers")
        raw_bridge_client = bridge_client._bridge_client
        assert raw_bridge_client

        # Create bridge worker last. We have empirically observed that if it is
        # created before an error is raised from the activity worker
        # constructor, a deadlock/hang will occur presumably while trying to
        # free it.
        # TODO(cretz): Why does this cause a test hang when an exception is
        # thrown after it?
        self._bridge_worker = temporalio.bridge.worker.Worker.create(
            raw_bridge_client,
            temporalio.bridge.worker.WorkerConfig(
                namespace=client.namespace,
                task_queue=task_queue,
                build_id=build_id or load_default_build_id(),
                identity_override=identity,
                max_cached_workflows=max_cached_workflows,
                max_outstanding_workflow_tasks=max_concurrent_workflow_tasks,
                max_outstanding_activities=max_concurrent_activities,
                max_outstanding_local_activities=max_concurrent_local_activities,
                max_concurrent_workflow_task_polls=max_concurrent_workflow_task_polls,
                nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
                max_concurrent_activity_task_polls=max_concurrent_activity_task_polls,
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
                max_activities_per_second=max_activities_per_second,
                max_task_queue_activities_per_second=max_task_queue_activities_per_second,
            ),
        )

    def config(self) -> WorkerConfig:
        """Config, as a dictionary, used to create this worker.

        Returns:
            Configuration, shallow-copied.
        """
        config = self._config.copy()
        config["activities"] = list(config["activities"])
        config["workflows"] = list(config["workflows"])
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
        worker_tasks: List[asyncio.Task] = []
        if self._activity_worker:
            worker_tasks.append(asyncio.create_task(self._activity_worker.run()))
        if self._workflow_worker:
            worker_tasks.append(asyncio.create_task(self._workflow_worker.run()))
        self._task = asyncio.create_task(asyncio.wait(worker_tasks))
        return self._task

    async def shutdown(self) -> None:
        """Shutdown the worker and wait until all activities have completed.

        This will initiate a shutdown and optionally wait for a grace period
        before sending cancels to all activities.

        This worker should not be used in any way once this is called.
        """
        if not self._task:
            raise RuntimeError("Never started")
        graceful_timeout = self._config["graceful_shutdown_timeout"]
        logger.info(
            f"Beginning worker shutdown, will wait {graceful_timeout} before cancelling workflows/activities"
        )
        # Start shutdown of the bridge
        bridge_shutdown_task = asyncio.create_task(self._bridge_worker.shutdown())
        # Wait for the poller loops to stop
        await self._task
        # Shutdown the activity worker (there is no workflow worker shutdown)
        if self._activity_worker:
            await self._activity_worker.shutdown(graceful_timeout)
        # Wait for the bridge to report everything is completed
        await bridge_shutdown_task
        # Do final shutdown
        await self._bridge_worker.finalize_shutdown()


class WorkerConfig(TypedDict, total=False):
    """TypedDict of config originally passed to :py:class:`Worker`."""

    client: temporalio.client.Client
    task_queue: str
    activities: Sequence[Callable]
    workflows: Sequence[Type]
    activity_executor: Optional[concurrent.futures.Executor]
    workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor]
    workflow_runner: WorkflowRunner
    interceptors: Sequence[Interceptor]
    build_id: Optional[str]
    identity: Optional[str]
    max_cached_workflows: int
    max_concurrent_workflow_tasks: int
    max_concurrent_activities: int
    max_concurrent_local_activities: int
    max_concurrent_workflow_task_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_activity_task_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout: timedelta
    max_heartbeat_throttle_interval: timedelta
    default_heartbeat_throttle_interval: timedelta
    max_activities_per_second: Optional[float]
    max_task_queue_activities_per_second: Optional[float]
    graceful_shutdown_timeout: timedelta
    shared_state_manager: Optional[SharedStateManager]
    debug_mode: bool


_default_build_id: Optional[str] = None


def load_default_build_id(*, memoize: bool = True) -> str:
    """Load the default worker build ID.

    The worker build ID is a unique hash representing the entire set of code
    including Temporal code and external code. The default here is currently
    implemented by walking loaded modules and hashing their bytecode into a
    common hash.

    Args:
        memoize: If true, the default, this will cache to a global variable to
            keep from having to run again on successive calls.

    Returns:
        Unique identifier representing the set of running code.
    """
    # Memoize
    global _default_build_id
    if memoize and _default_build_id:
        return _default_build_id

    # The goal is to get a hash representing the set of runtime code, both
    # Temporal's and the user's. After all options were explored, we have
    # decided to default to hashing all bytecode of imported modules. We accept
    # that this has the following limitations:
    #
    # * Dynamic imports later on can affect this value
    # * Dynamic imports based on env var, platform, etc can affect this value
    # * Using the loader's get_code seems to use get_data which does a disk read
    # * Using the loader's get_code in rare cases can cause a compile()

    got_temporal_code = False
    m = hashlib.md5()
    for mod_name in sorted(sys.modules):
        # Try to read code
        code = _get_module_code(mod_name)
        if not code:
            continue
        if mod_name == "temporalio":
            got_temporal_code = True
        # Add to MD5 digest
        m.update(code)
    # If we didn't even get the temporalio module from this approach, this
    # approach is flawed and we prefer to return an error forcing user to
    # explicitly set the worker binary ID instead of silently using a value that
    # may never change
    if not got_temporal_code:
        raise RuntimeError(
            "Cannot get default unique worker binary ID, the value should be explicitly set"
        )
    # Return the hex digest
    digest = m.hexdigest()
    if memoize:
        _default_build_id = digest
    return digest


def _get_module_code(mod_name: str) -> Optional[bytes]:
    # First try the module's loader and if that fails, try __cached__ file
    try:
        loader: Any = sys.modules[mod_name].__loader__
        code = loader.get_code(mod_name).co_code
        if code:
            return code
    except Exception:
        pass
    try:
        # Technically we could read smaller chunks per file here and update the
        # hash, but the benefit is negligible especially since many non-built-in
        # modules will use get_code above
        with open(sys.modules[mod_name].__cached__, "rb") as f:
            return f.read()
    except Exception:
        pass
    return None
