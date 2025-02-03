"""Worker for processing Temporal workflows and/or activities."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import sys
import warnings
from datetime import timedelta
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Type, cast

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
import temporalio.runtime
import temporalio.service

from ._activity import SharedStateManager, _ActivityWorker
from ._interceptor import Interceptor
from ._tuning import WorkerTuner, _to_bridge_slot_supplier
from ._workflow import _WorkflowWorker
from ._workflow_instance import UnsandboxedWorkflowRunner, WorkflowRunner
from .workflow_sandbox import SandboxedWorkflowRunner

logger = logging.getLogger(__name__)


class Worker:
    """Worker to process workflows and/or activities.

    Once created, workers can be run and shutdown explicitly via :py:meth:`run`
    and :py:meth:`shutdown`. Alternatively workers can be used in an
    ``async with`` clause. See :py:meth:`__aenter__` and :py:meth:`__aexit__`
    for important details about fatal errors.
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
        workflow_runner: WorkflowRunner = SandboxedWorkflowRunner(),
        unsandboxed_workflow_runner: WorkflowRunner = UnsandboxedWorkflowRunner(),
        interceptors: Sequence[Interceptor] = [],
        build_id: Optional[str] = None,
        identity: Optional[str] = None,
        max_cached_workflows: int = 1000,
        max_concurrent_workflow_tasks: Optional[int] = None,
        max_concurrent_activities: Optional[int] = None,
        max_concurrent_local_activities: Optional[int] = None,
        tuner: Optional[WorkerTuner] = None,
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
        workflow_failure_exception_types: Sequence[Type[BaseException]] = [],
        shared_state_manager: Optional[SharedStateManager] = None,
        debug_mode: bool = False,
        disable_eager_activity_execution: bool = False,
        on_fatal_error: Optional[Callable[[BaseException], Awaitable[None]]] = None,
        use_worker_versioning: bool = False,
        disable_safe_workflow_eviction: bool = False,
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
                activities. This is required if any activities are non-async.
                :py:class:`concurrent.futures.ThreadPoolExecutor` is
                recommended. If this is a
                :py:class:`concurrent.futures.ProcessPoolExecutor`, all
                non-async activities must be picklable. ``max_workers`` on the
                executor should at least be ``max_concurrent_activities`` or a
                warning is issued. Note, a broken-executor failure from this
                executor will cause the worker to fail and shutdown.
            workflow_task_executor: Thread pool executor for workflow tasks. If
                this is not present, a new
                :py:class:`concurrent.futures.ThreadPoolExecutor` will be
                created with ``max_workers`` set to ``max(os.cpu_count(), 4)``.
                The default one will be properly shutdown, but if one is
                provided, the caller is responsible for shutting it down after
                the worker is shut down.
            workflow_runner: Runner for workflows.
            unsandboxed_workflow_runner: Runner for workflows that opt-out of
                sandboxing.
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
                tasks that will ever be given to this worker at one time. Mutually exclusive with ``tuner``.
            max_concurrent_activities: Maximum number of activity tasks that
                will ever be given to this worker concurrently. Mutually exclusive with ``tuner``.
            max_concurrent_local_activities: Maximum number of local activity
                tasks that will ever be given to this worker concurrently. Mutually exclusive with ``tuner``.
            tuner:  Provide a custom :py:class:`WorkerTuner`. Mutually exclusive with the
                ``max_concurrent_workflow_tasks``, ``max_concurrent_activities``, and
                ``max_concurrent_local_activities`` arguments.

                WARNING: This argument is experimental
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
            workflow_failure_exception_types: The types of exceptions that, if a
                workflow-thrown exception extends, will cause the
                workflow/update to fail instead of suspending the workflow via
                task failure. These are applied in addition to ones set on the
                ``workflow.defn`` decorator. If ``Exception`` is set, it
                effectively will fail a workflow/update in all user exception
                cases. WARNING: This setting is experimental.
            shared_state_manager: Used for obtaining cross-process friendly
                synchronization primitives. This is required for non-async
                activities where the activity_executor is not a
                :py:class:`concurrent.futures.ThreadPoolExecutor`. Reuse of
                these across workers is encouraged.
            debug_mode: If true, will disable deadlock detection and may disable
                sandboxing in order to make using a debugger easier. If false
                but the environment variable ``TEMPORAL_DEBUG`` is truthy, this
                will be set to true.
            disable_eager_activity_execution: If true, will disable eager
                activity execution. Eager activity execution is an optimization
                on some servers that sends activities back to the same worker as
                the calling workflow if they can run there. This setting is
                experimental and may be removed in a future release.
            on_fatal_error: An async function that can handle a failure before
                the worker shutdown commences. This cannot stop the shutdown and
                any exception raised is logged and ignored.
            use_worker_versioning: If true, the `build_id` argument must be
                specified, and this worker opts into the worker versioning
                feature. This ensures it only receives workflow tasks for
                workflows which it claims to be compatible with. For more
                information, see
                https://docs.temporal.io/workers#worker-versioning.
            disable_safe_workflow_eviction: If true, instead of letting the
                workflow collect its tasks properly, the worker will simply let
                the Python garbage collector collect the tasks. WARNING: Users
                should not set this value to true. The garbage collector will
                throw ``GeneratorExit`` in coroutines causing them to wake up
                in different threads and run ``finally`` and other code in the
                wrong workflow environment.
        """
        if not activities and not workflows:
            raise ValueError("At least one activity or workflow must be specified")
        if use_worker_versioning and not build_id:
            raise ValueError(
                "build_id must be specified when use_worker_versioning is True"
            )

        # Prepend applicable client interceptors to the given ones
        client_config = client.config()
        interceptors_from_client = cast(
            List[Interceptor],
            [i for i in client_config["interceptors"] if isinstance(i, Interceptor)],
        )
        interceptors = interceptors_from_client + list(interceptors)

        # Extract the bridge service client
        bridge_client = _extract_bridge_client_for_worker(client)

        # Store the config for tracking
        self._config = WorkerConfig(
            client=client,
            task_queue=task_queue,
            activities=activities,
            workflows=workflows,
            activity_executor=activity_executor,
            workflow_task_executor=workflow_task_executor,
            workflow_runner=workflow_runner,
            unsandboxed_workflow_runner=unsandboxed_workflow_runner,
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
            workflow_failure_exception_types=workflow_failure_exception_types,
            shared_state_manager=shared_state_manager,
            debug_mode=debug_mode,
            disable_eager_activity_execution=disable_eager_activity_execution,
            on_fatal_error=on_fatal_error,
            use_worker_versioning=use_worker_versioning,
            disable_safe_workflow_eviction=disable_safe_workflow_eviction,
        )
        self._started = False
        self._shutdown_event = asyncio.Event()
        self._shutdown_complete_event = asyncio.Event()
        self._async_context_inner_task: Optional[asyncio.Task] = None
        self._async_context_run_task: Optional[asyncio.Task] = None
        self._async_context_run_exception: Optional[BaseException] = None

        # Create activity and workflow worker
        self._activity_worker: Optional[_ActivityWorker] = None
        self._runtime = (
            bridge_client.config.runtime or temporalio.runtime.Runtime.default()
        )
        if activities:
            # Issue warning here if executor max_workers is lower than max
            # concurrent activities. We do this here instead of in
            # _ActivityWorker so the stack level is predictable.
            max_workers = getattr(activity_executor, "_max_workers", None)
            concurrent_activities = max_concurrent_activities
            if tuner and tuner._get_activities_max():
                concurrent_activities = tuner._get_activities_max()
            if isinstance(max_workers, int) and max_workers < (
                concurrent_activities or 0
            ):
                warnings.warn(
                    f"Worker max_concurrent_activities is {concurrent_activities} "
                    + f"but activity_executor's max_workers is only {max_workers}",
                    stacklevel=2,
                )

            self._activity_worker = _ActivityWorker(
                bridge_worker=lambda: self._bridge_worker,
                task_queue=task_queue,
                activities=activities,
                activity_executor=activity_executor,
                shared_state_manager=shared_state_manager,
                data_converter=client_config["data_converter"],
                interceptors=interceptors,
                metric_meter=self._runtime.metric_meter,
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
                unsandboxed_workflow_runner=unsandboxed_workflow_runner,
                data_converter=client_config["data_converter"],
                interceptors=interceptors,
                workflow_failure_exception_types=workflow_failure_exception_types,
                debug_mode=debug_mode,
                disable_eager_activity_execution=disable_eager_activity_execution,
                metric_meter=self._runtime.metric_meter,
                on_eviction_hook=None,
                disable_safe_eviction=disable_safe_workflow_eviction,
            )

        if tuner is not None:
            if (
                max_concurrent_workflow_tasks
                or max_concurrent_activities
                or max_concurrent_local_activities
            ):
                raise ValueError(
                    "Cannot specify max_concurrent_workflow_tasks, max_concurrent_activities, "
                    "or max_concurrent_local_activities when also specifying tuner"
                )
        else:
            tuner = WorkerTuner.create_fixed(
                workflow_slots=max_concurrent_workflow_tasks,
                activity_slots=max_concurrent_activities,
                local_activity_slots=max_concurrent_local_activities,
            )

        bridge_tuner = tuner._to_bridge_tuner()

        # Create bridge worker last. We have empirically observed that if it is
        # created before an error is raised from the activity worker
        # constructor, a deadlock/hang will occur presumably while trying to
        # free it.
        # TODO(cretz): Why does this cause a test hang when an exception is
        # thrown after it?
        assert bridge_client._bridge_client
        self._bridge_worker = temporalio.bridge.worker.Worker.create(
            bridge_client._bridge_client,
            temporalio.bridge.worker.WorkerConfig(
                namespace=client.namespace,
                task_queue=task_queue,
                build_id=build_id or load_default_build_id(),
                identity_override=identity,
                max_cached_workflows=max_cached_workflows,
                tuner=bridge_tuner,
                max_concurrent_workflow_task_polls=max_concurrent_workflow_task_polls,
                nonsticky_to_sticky_poll_ratio=nonsticky_to_sticky_poll_ratio,
                max_concurrent_activity_task_polls=max_concurrent_activity_task_polls,
                # We have to disable remote activities if a user asks _or_ if we
                # are not running an activity worker at all. Otherwise shutdown
                # will not proceed properly.
                no_remote_activities=no_remote_activities or not activities,
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
                graceful_shutdown_period_millis=int(
                    1000 * graceful_shutdown_timeout.total_seconds()
                ),
                use_worker_versioning=use_worker_versioning,
                # Need to tell core whether we want to consider all
                # non-determinism exceptions as workflow fail, and whether we do
                # per workflow type
                nondeterminism_as_workflow_fail=self._workflow_worker is not None
                and self._workflow_worker.nondeterminism_as_workflow_fail(),
                nondeterminism_as_workflow_fail_for_types=(
                    self._workflow_worker.nondeterminism_as_workflow_fail_for_types()
                    if self._workflow_worker
                    else set()
                ),
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

    @property
    def client(self) -> temporalio.client.Client:
        """Client currently set on the worker."""
        return self._config["client"]

    @client.setter
    def client(self, value: temporalio.client.Client) -> None:
        """Update the client associated with the worker.

        Changing the client will make sure the worker starts using it for the
        next calls it makes. However, outstanding client calls will still
        complete with the existing client. The new client cannot be "lazy" and
        must be using the same runtime as the current client.
        """
        bridge_client = _extract_bridge_client_for_worker(value)
        if self._runtime is not bridge_client.config.runtime:
            raise ValueError(
                "New client is not on the same runtime as the existing client"
            )
        assert bridge_client._bridge_client
        self._bridge_worker.replace_client(bridge_client._bridge_client)
        self._config["client"] = value

    @property
    def is_running(self) -> bool:
        """Whether the worker is running.

        This is only ``True`` if the worker has been started and not yet
        shut down.
        """
        return self._started and not self.is_shutdown

    @property
    def is_shutdown(self) -> bool:
        """Whether the worker has run and shut down.

        This is only ``True`` if the worker was once started and then shutdown.
        This is not necessarily ``True`` after :py:meth:`shutdown` is first
        called because the shutdown process can take a bit.
        """
        return self._shutdown_complete_event.is_set()

    async def run(self) -> None:
        """Run the worker and wait on it to be shut down.

        This will not return until shutdown is complete. This means that
        activities have all completed after being told to cancel after the
        graceful timeout period.

        This method will raise if there is a worker fatal error. While
        :py:meth:`shutdown` does not need to be invoked in this case, it is
        harmless to do so. Otherwise, to shut down this worker, invoke
        :py:meth:`shutdown`.

        Technically this worker can be shutdown by issuing a cancel to this
        async function assuming that it is currently running. A cancel could
        also cancel the shutdown process. Therefore users are encouraged to use
        explicit shutdown instead.
        """
        # Eagerly validate which will do a namespace check in Core
        await self._bridge_worker.validate()

        if self._started:
            raise RuntimeError("Already started")
        self._started = True

        # Create a task that raises when a shutdown is requested
        async def raise_on_shutdown():
            try:
                await self._shutdown_event.wait()
                raise _ShutdownRequested()
            except asyncio.CancelledError:
                pass

        tasks: List[asyncio.Task] = [asyncio.create_task(raise_on_shutdown())]
        # Create tasks for workers
        if self._activity_worker:
            tasks.append(asyncio.create_task(self._activity_worker.run()))
        if self._workflow_worker:
            tasks.append(asyncio.create_task(self._workflow_worker.run()))

        # Wait for either worker or shutdown requested
        wait_task = asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        try:
            await asyncio.shield(wait_task)

            # If any of the last two tasks failed, we want to re-raise that as
            # the exception
            exception = next((t.exception() for t in tasks[1:] if t.done()), None)
            if exception:
                logger.error("Worker failed, shutting down", exc_info=exception)
                if self._config["on_fatal_error"]:
                    try:
                        await self._config["on_fatal_error"](exception)
                    except:
                        logger.warning("Fatal error handler failed")

        except asyncio.CancelledError as user_cancel_err:
            # Represents user literally calling cancel
            logger.info("Worker cancelled, shutting down")
            exception = user_cancel_err

        # Cancel the shutdown task (safe if already done)
        tasks[0].cancel()
        graceful_timeout = self._config["graceful_shutdown_timeout"]
        logger.info(
            f"Beginning worker shutdown, will wait {graceful_timeout} before cancelling activities"
        )

        # Initiate core worker shutdown
        self._bridge_worker.initiate_shutdown()

        # If any worker task had an exception, replace that task with a queue
        # drain (task at index 1 can be activity or workflow worker, task at
        # index 2 must be workflow worker if present)
        if tasks[1].done() and tasks[1].exception():
            if self._activity_worker:
                tasks[1] = asyncio.create_task(self._activity_worker.drain_poll_queue())
            else:
                assert self._workflow_worker
                tasks[1] = asyncio.create_task(self._workflow_worker.drain_poll_queue())
        if len(tasks) > 2 and tasks[2].done() and tasks[2].exception():
            assert self._workflow_worker
            tasks[2] = asyncio.create_task(self._workflow_worker.drain_poll_queue())

        # Notify shutdown occurring
        if self._activity_worker:
            self._activity_worker.notify_shutdown()
        if self._workflow_worker:
            self._workflow_worker.notify_shutdown()

        # Wait for all tasks to complete (i.e. for poller loops to stop)
        await asyncio.wait(tasks)
        # Sometimes both workers throw an exception and since we only take the
        # first, Python may complain with "Task exception was never retrieved"
        # if we don't get the others. Therefore we call cancel on each task
        # which suppresses this.
        for task in tasks:
            task.cancel()

        # If there's an activity worker, we have to let all activity completions
        # finish. We cannot guarantee that because poll shutdown completed
        # (which means activities completed) that they got flushed to the
        # server.
        if self._activity_worker:
            await self._activity_worker.wait_all_completed()

        # Do final shutdown
        try:
            await self._bridge_worker.finalize_shutdown()
        except:
            # Ignore errors here that can arise in some tests where the bridge
            # worker still has a reference
            pass

        # Mark as shutdown complete and re-raise exception if present
        self._shutdown_complete_event.set()
        if exception:
            raise exception

    async def shutdown(self) -> None:
        """Initiate a worker shutdown and wait until complete.

        This can be called before the worker has even started and is safe for
        repeated invocations. It simply sets a marker informing the worker to
        shut down as it runs.

        This will not return until the worker has completed shutting down.
        """
        self._shutdown_event.set()
        await self._shutdown_complete_event.wait()

    async def __aenter__(self) -> Worker:
        """Start the worker and return self for use by ``async with``.

        This is a wrapper around :py:meth:`run`. Please review that method.

        This takes a similar approach to :py:func:`asyncio.timeout` in that it
        will cancel the current task if there is a fatal worker error and raise
        that error out of the context manager. However, if the inner async code
        swallows/wraps the :py:class:`asyncio.CancelledError`, the exiting
        portion of the context manager will not raise the fatal worker error.
        """
        if self._async_context_inner_task:
            raise RuntimeError("Already started")
        self._async_context_inner_task = asyncio.current_task()
        if not self._async_context_inner_task:
            raise RuntimeError("Can only use async with inside a task")

        # Start a task that runs and if there's an error, cancels the current
        # task and re-raises the error
        async def run():
            try:
                await self.run()
            except BaseException as err:
                self._async_context_run_exception = err
                self._async_context_inner_task.cancel()  # type: ignore[union-attr]

        self._async_context_run_task = asyncio.create_task(run())
        return self

    async def __aexit__(self, exc_type: Optional[Type[BaseException]], *args) -> None:
        """Same as :py:meth:`shutdown` for use by ``async with``.

        Note, this will raise the worker fatal error if one occurred and the
        inner task cancellation was not inadvertently swallowed/wrapped.
        """
        # Wait for shutdown then run complete
        if not self._async_context_run_task:
            raise RuntimeError("Never started")
        await self.shutdown()
        # Cancel our run task
        self._async_context_run_task.cancel()
        # Only re-raise our exception if present and exc_type is cancel
        if exc_type is asyncio.CancelledError and self._async_context_run_exception:
            raise self._async_context_run_exception


class WorkerConfig(TypedDict, total=False):
    """TypedDict of config originally passed to :py:class:`Worker`."""

    client: temporalio.client.Client
    task_queue: str
    activities: Sequence[Callable]
    workflows: Sequence[Type]
    activity_executor: Optional[concurrent.futures.Executor]
    workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor]
    workflow_runner: WorkflowRunner
    unsandboxed_workflow_runner: WorkflowRunner
    interceptors: Sequence[Interceptor]
    build_id: Optional[str]
    identity: Optional[str]
    max_cached_workflows: int
    max_concurrent_workflow_tasks: Optional[int]
    max_concurrent_activities: Optional[int]
    max_concurrent_local_activities: Optional[int]
    tuner: Optional[WorkerTuner]
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
    workflow_failure_exception_types: Sequence[Type[BaseException]]
    shared_state_manager: Optional[SharedStateManager]
    debug_mode: bool
    disable_eager_activity_execution: bool
    on_fatal_error: Optional[Callable[[BaseException], Awaitable[None]]]
    use_worker_versioning: bool
    disable_safe_workflow_eviction: bool


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
    if sys.version_info < (3, 9):
        m = hashlib.md5()
    else:
        m = hashlib.md5(usedforsecurity=False)
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


def _extract_bridge_client_for_worker(
    client: temporalio.client.Client,
) -> temporalio.service._BridgeServiceClient:
    # Extract the bridge service client. We try the service on the client first,
    # then we support a worker_service_client on the client's service to return
    # underlying service client we can use.
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

    # We need an already connected client
    # TODO(cretz): How to connect to client inside Worker constructor here? In
    # the meantime, we disallow lazy clients from being used for workers. We
    # could check whether the connected client is present which means
    # lazy-but-already-connected clients would work, but that is confusing
    # to users that the client only works if they already made a call on it.
    if bridge_client.config.lazy:
        raise RuntimeError("Lazy clients cannot be used for workers")
    return bridge_client


class _ShutdownRequested(RuntimeError):
    pass
