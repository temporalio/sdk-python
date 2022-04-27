from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextvars
import inspect
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tkinter import E
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Generator,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeAlias,
    TypeVar,
    Union,
    cast,
)

import google.protobuf.timestamp_pb2

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow
import temporalio.workflow_service

from .interceptor import (
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)

logger = logging.getLogger(__name__)

LOG_PROTOS = False
DEADLOCK_TIMEOUT_SECONDS = 2


@dataclass
class _WorkflowWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        namespace: str,
        task_queue: str,
        workflows: Iterable[Type],
        workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor],
        data_converter: temporalio.converter.DataConverter,
        interceptors: Iterable[Interceptor],
        type_lookup: temporalio.converter._FunctionTypeLookup,
        max_concurrent_workflow_tasks: int,
    ) -> None:
        self._bridge_worker = bridge_worker
        self._namespace = namespace
        self._task_queue = task_queue
        self._workflow_task_executor = (
            workflow_task_executor
            or concurrent.futures.ThreadPoolExecutor(
                max_workers=max_concurrent_workflow_tasks,
                thread_name_prefix="temporal_workflow_",
            )
        )
        self._workflow_task_executor_user_provided = workflow_task_executor is not None
        self._data_converter = data_converter
        self._interceptors = interceptors
        self._type_lookup = type_lookup
        self._running_workflows: Dict[str, _RunningWorkflow] = {}

        # Validate and build workflow dict
        self._workflows: Dict[str, temporalio.workflow._Definition] = {}
        for workflow in workflows:
            defn = temporalio.workflow._Definition.must_from_class(workflow)
            # Confirm name unique
            if defn.name in self._workflows:
                raise ValueError(f"More than one workflow named {defn.name}")
            self._workflows[defn.name] = defn

    async def run(self) -> None:
        # Continually poll for workflow work
        task_tag = object()
        try:
            while True:
                act = await self._bridge_worker().poll_workflow_activation()
                # Schedule this as a task, but we don't need to track it or
                # await it. Rather we'll give it an attribute and wait for it
                # when done.
                task = asyncio.create_task(self._handle_activation(act))
                setattr(task, "__temporal_task_tag", task_tag)
        except temporalio.bridge.worker.PollShutdownError:
            return
        except Exception:
            # Should never happen
            logger.exception(f"Workflow runner failed")
        finally:
            # Collect all tasks and wait for them to complete
            our_tasks = [
                t
                for t in asyncio.all_tasks()
                if getattr(t, "__temporal_task_tag", None) is task_tag
            ]
            await asyncio.wait(our_tasks)
            # Shutdown the thread pool executor if we created it
            if not self._workflow_task_executor_user_provided:
                self._workflow_task_executor.shutdown()

    async def _handle_activation(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> None:
        global LOG_PROTOS
        if LOG_PROTOS:
            logger.debug("Received activation: %s", act)
        completion = (
            temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion(
                run_id=act.run_id
            )
        )

        # Build completion
        try:
            # If the workflow is not running yet, create it
            workflow = self._running_workflows.get(act.run_id)
            if not workflow:
                # First find the start workflow job
                start_job = next(
                    (j for j in act.jobs if j.HasField("start_workflow")), None
                )
                if not start_job:
                    raise RuntimeError("Missing start workflow")

                # Get the definition and create a workflow
                defn = self._workflows.get(start_job.start_workflow.workflow_type)
                if not defn:
                    workflow_names = ", ".join(sorted(self._workflows.keys()))
                    raise temporalio.exceptions.ApplicationError(
                        f"Workflow class {start_job.start_workflow.workflow_type} is not registered on this worker, available workflows: {workflow_names}",
                        type="NotFoundError",
                    )
                workflow = _RunningWorkflow.create(
                    self, defn, act, start_job.start_workflow
                )
                self._running_workflows[act.run_id] = workflow

            # Run activation and set successful commands
            commands = await workflow.activate(act)
            # TODO(cretz): Is this copy too expensive?
            completion.successful.commands.extend(commands)
        except Exception as err:
            logger.exception(f"Failed activation on workflow with run ID {act.run_id}")
            # Set completion failure
            completion.failed.failure.SetInParent()
            try:
                await temporalio.exceptions.apply_exception_to_failure(
                    err, self._data_converter, completion.failed.failure
                )
            except Exception as inner_err:
                logger.exception(
                    f"Failed converting activation exception on workflow with run ID {act.run_id}"
                )
                completion.failed.failure.message = (
                    f"Failed converting activation exception: {inner_err}"
                )

        # Send off completion
        if LOG_PROTOS:
            logger.debug("Sending completion: %s", completion)
        try:
            await self._bridge_worker().complete_workflow_activation(completion)
        except Exception:
            # TODO(cretz): Per others, this is supposed to crash the worker
            logger.exception(
                f"Failed completing activation on workflow with run ID {act.run_id}"
            )

        # If there is a remove-from-cache job, do so
        remove_job = next(
            (j for j in act.jobs if j.HasField("remove_from_cache")), None
        )
        if remove_job:
            logger.debug(
                f"Evicting workflow with run ID {act.run_id}, message: {remove_job.remove_from_cache.message}"
            )
            del self._running_workflows[act.run_id]

    async def _convert_args(
        self,
        payloads: Sequence[temporalio.bridge.proto.common.Payload],
        arg_types: Optional[List[Type]],
    ) -> List[Any]:
        if not payloads:
            return []
        # Only use type hints if they match count
        if arg_types and len(arg_types) != len(payloads):
            arg_types = None
        try:
            return await self._data_converter.decode(
                temporalio.bridge.worker.from_bridge_payloads(payloads),
                type_hints=arg_types,
            )
        except Exception as err:
            raise RuntimeError("Failed decoding arguments") from err


# Command can either be a fixed command or a callback
_PendingCommand: TypeAlias = Union[
    temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    Callable[[], Awaitable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]],
]


class _RunningWorkflow:
    @staticmethod
    def create(
        worker: _WorkflowWorker,
        defn: temporalio.workflow._Definition,
        act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
        start: temporalio.bridge.proto.workflow_activation.StartWorkflow,
    ) -> _RunningWorkflow:
        # Create the info
        info = temporalio.workflow.Info(
            attempt=start.attempt,
            cron_schedule=start.cron_schedule or None,
            execution_timeout=start.workflow_execution_timeout.ToTimedelta()
            if start.HasField("workflow_execution_timeout")
            else None,
            namespace=worker._namespace,
            run_id=act.run_id,
            run_timeout=start.workflow_run_timeout.ToTimedelta()
            if start.HasField("workflow_run_timeout")
            else None,
            start_time=_proto_to_datetime(act.timestamp),
            task_queue=worker._task_queue,
            task_timeout=start.workflow_task_timeout.ToTimedelta(),
            workflow_id=start.workflow_id,
            workflow_type=start.workflow_type,
        )

        # Create the running workflow
        instance = defn.cls()
        return _RunningWorkflow(worker, defn, info, instance)

    def __init__(
        self,
        worker: _WorkflowWorker,
        defn: temporalio.workflow._Definition,
        info: temporalio.workflow.Info,
        instance: Any,
    ) -> None:
        self._worker = worker
        self._defn = defn
        self._info = info
        self._instance = instance
        # We ignore MyPy failing to instantiate this because it's not _really_
        # abstract at runtime
        self._loop = _EventLoop(self)  # type: ignore[abstract]
        self._pending_commands: List[_PendingCommand] = []

        # We maintain signals and queries on this class since handlers can be
        # added during workflow execution
        self._signals = dict(defn.signals)
        self._queries = dict(defn.queries)

        # Maintain buffered signals for later-added dynamic handlers
        self._buffered_signals: Dict[
            str, List[temporalio.bridge.proto.workflow_activation.SignalWorkflow]
        ] = {}

        # Create runtime w/ default outbound so that it can be used during
        # interceptor init. Also set it on the loop.
        self._runtime = _WorkflowRuntimeImpl(self, _WorkflowOutboundImpl(self))
        temporalio.workflow._Runtime.set_on_loop(self._loop, self._runtime)
        try:
            # Set on this loop for use by interceptor init
            temporalio.workflow._Runtime.set_on_loop(
                asyncio.get_running_loop(), self._runtime
            )

            # Init the interceptor
            root_inbound = _WorkflowInboundImpl(self)
            self._inbound: WorkflowInboundInterceptor = root_inbound
            for interceptor in reversed(list(worker._interceptors)):
                self._inbound = interceptor.intercept_workflow(self._inbound)
            self._inbound.init(self._runtime._outbound)

            # Change the runtime's outbound
            self._runtime._outbound = root_inbound._outbound
        finally:
            # Remove off loop
            temporalio.workflow._Runtime.set_on_loop(asyncio.get_running_loop(), None)

    async def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> Iterable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]:
        global DEADLOCK_TIMEOUT_SECONDS

        self._pending_commands = []

        # TODO(cretz): Apparently this can go backwards, but Python expects it
        # to be monotonic
        self._loop.set_time(act.timestamp.ToMicroseconds() / 1e6)

        # Split into job sets with patches, then signals, then non-queries, then
        # queries
        job_sets: List[
            List[temporalio.bridge.proto.workflow_activation.WorkflowActivationJob]
        ] = [[], [], [], []]
        for job in act.jobs:
            if job.HasField("notify_has_patch"):
                job_sets[0].append(job)
            elif job.HasField("signal_workflow"):
                job_sets[1].append(job)
            elif not job.HasField("query_workflow"):
                job_sets[2].append(job)
            else:
                job_sets[3].append(job)

        # Apply jobs
        for job_set in job_sets:
            if not job_set:
                continue
            for job in job_set:
                # Let errors bubble out of these to the caller to fail the task
                if job.HasField("fire_timer"):
                    await self._fire_timer(job.fire_timer)
                elif job.HasField("query_workflow"):
                    await self._query_workflow(job.query_workflow)
                elif job.HasField("remove_from_cache"):
                    # Ignore, handled by _handle_activation
                    pass
                elif job.HasField("resolve_activity"):
                    await self._resolve_activity(job.resolve_activity)
                elif job.HasField("resolve_child_workflow_execution"):
                    await self._resolve_child_workflow_execution(
                        job.resolve_child_workflow_execution
                    )
                elif job.HasField("resolve_child_workflow_execution_start"):
                    await self._resolve_child_workflow_execution_start(
                        job.resolve_child_workflow_execution_start
                    )
                elif job.HasField("signal_workflow"):
                    await self._signal_workflow(job.signal_workflow)
                elif job.HasField("start_workflow"):
                    await self._start_workflow(job.start_workflow)
                else:
                    print(f"TODO(cretz) JOB: {job.WhichOneof('variant')}")
                    # raise RuntimeError(f"unrecognized job: {job.WhichOneof('variant')}")

            # Run a loop iteration on a thread. We will throw a deadlock
            # detected error if it exceeds the timeout
            exec_task = asyncio.get_running_loop().run_in_executor(
                self._worker._workflow_task_executor,
                self._loop.run_once,
            )
            try:
                await asyncio.wait_for(exec_task, DEADLOCK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Potential deadlock detected, workflow didn't yield within {DEADLOCK_TIMEOUT_SECONDS} second(s)"
                )

        # Apply all collected commands
        commands: List[temporalio.bridge.proto.workflow_commands.WorkflowCommand] = []
        for pending_command in self._pending_commands:
            if callable(pending_command):
                commands.append(await pending_command())
            else:
                commands.append(pending_command)
        return commands

    async def _fire_timer(
        self, job: temporalio.bridge.proto.workflow_activation.FireTimer
    ) -> None:
        self._loop.resolve_timer(job.seq)

    async def _query_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.QueryWorkflow
    ) -> None:
        # Command builder call after query completes to run in this event
        # loop
        success: Optional[Any] = None
        failure: Optional[Exception] = None

        async def handle_complete() -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
            nonlocal success, failure
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            command.respond_to_query.query_id = job.query_id
            if failure:
                command.respond_to_query.failed.SetInParent()
                try:
                    await temporalio.exceptions.apply_exception_to_failure(
                        failure,
                        self._worker._data_converter,
                        command.fail_workflow_execution.failure,
                    )
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting application error"
                    ) from inner_err
            else:
                result_payloads = await self._worker._data_converter.encode([success])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command.respond_to_query.succeeded.response.CopyFrom(
                    temporalio.bridge.worker.to_bridge_payload(result_payloads[0])
                )
            return command

        # Async call to run on the scheduler thread
        async def run_query(input: HandleQueryInput) -> None:
            nonlocal success, failure
            try:
                success = await self._inbound.handle_query(input)
            except Exception as err:
                failure = err
            self._pending_commands.append(handle_complete)

        # Just find the arg types for now. The interceptor will be responsible
        # for checking whether the query definition actually exists.
        query_defn = self._defn.queries.get(job.query_type)
        arg_types, _ = (
            self._worker._type_lookup.get_type_hints(query_defn.fn)
            if query_defn
            else (None, None)
        )
        args = await self._worker._convert_args(job.arguments, arg_types)

        # Schedule it
        input = HandleQueryInput(
            id=job.query_id,
            name=job.query_type,
            args=args,
        )
        self._loop.create_task(run_query(input))

    async def _resolve_activity(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveActivity
    ) -> None:
        if job.result.HasField("completed"):
            # Get the pending activity out of the loop so we can have the return
            # type definition
            handle = self._loop._pending_activities.get(job.seq)
            if not handle:
                raise RuntimeError(
                    f"Failed finding activity handle for sequence {job.seq}"
                )
            ret: Optional[Any] = None
            if job.result.completed.HasField("result"):
                ret_types = [handle.ret_type] if handle.ret_type else None
                ret_vals = await self._worker._data_converter.decode(
                    [
                        temporalio.bridge.worker.from_bridge_payload(
                            job.result.completed.result
                        )
                    ],
                    ret_types,
                )
                ret = ret_vals[0]
            self._loop.resolve_activity_success(job.seq, ret)
        elif job.result.HasField("failed"):
            exc = await temporalio.exceptions.failure_to_error(
                job.result.failed.failure, self._worker._data_converter
            )
            self._loop.resolve_activity_failure(job.seq, exc)
        elif job.result.HasField("cancelled"):
            exc = await temporalio.exceptions.failure_to_error(
                job.result.cancelled.failure, self._worker._data_converter
            )
            self._loop.resolve_activity_failure(job.seq, exc)
        else:
            raise RuntimeError("Activity did not have result")

    async def _resolve_child_workflow_execution(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveChildWorkflowExecution,
    ) -> None:
        if job.result.HasField("completed"):
            # Get the pending child workflow out of the loop so we can have the
            # return type definition
            handle = self._loop._pending_child_workflows.get(job.seq)
            if not handle:
                raise RuntimeError(
                    f"Failed finding child workflow handle for sequence {job.seq}"
                )
            ret: Optional[Any] = None
            if job.result.completed.HasField("result"):
                ret_types = [handle.ret_type] if handle.ret_type else None
                ret_vals = await self._worker._data_converter.decode(
                    [
                        temporalio.bridge.worker.from_bridge_payload(
                            job.result.completed.result
                        )
                    ],
                    ret_types,
                )
                ret = ret_vals[0]
            self._loop.resolve_child_workflow_success(job.seq, ret)
        elif job.result.HasField("failed"):
            exc = await temporalio.exceptions.failure_to_error(
                job.result.failed.failure, self._worker._data_converter
            )
            self._loop.resolve_child_workflow_failure(job.seq, exc)
        elif job.result.HasField("cancelled"):
            exc = await temporalio.exceptions.failure_to_error(
                job.result.cancelled.failure, self._worker._data_converter
            )
            self._loop.resolve_child_workflow_failure(job.seq, exc)
        else:
            raise RuntimeError("Child workflow did not have result")

    async def _resolve_child_workflow_execution_start(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveChildWorkflowExecutionStart,
    ) -> None:
        if job.HasField("succeeded"):
            self._loop.resolve_child_workflow_start_success(
                job.seq, job.succeeded.run_id
            )
        elif job.HasField("failed"):
            if (
                job.failed.cause
                != temporalio.bridge.proto.child_workflow.StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS
            ):
                raise ValueError(
                    f"Unexpected child start fail cause: {job.failed.cause}"
                )
            self._loop.resolve_child_workflow_start_failure(
                job.seq,
                temporalio.exceptions.WorkflowAlreadyStartedError(
                    job.failed.workflow_id, job.failed.workflow_type
                ),
            )
        elif job.HasField("cancelled"):
            exc = await temporalio.exceptions.failure_to_error(
                job.cancelled.failure, self._worker._data_converter
            )
            self._loop.resolve_child_workflow_start_failure(job.seq, exc)
        else:
            raise RuntimeError("Child workflow start did not have status")

    async def _signal_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.SignalWorkflow
    ) -> None:
        # If there is no definition or dynamic, we buffer and ignore
        if job.signal_name not in self._signals and None not in self._signals:
            self._buffered_signals.setdefault(job.signal_name, []).append(job)
            return

        # Just find the arg types for now. The interceptor will be responsible
        # for checking whether the query definition actually exists.
        signal_defn = self._defn.signals.get(job.signal_name)
        arg_types, _ = (
            self._worker._type_lookup.get_type_hints(signal_defn.fn)
            if signal_defn
            else (None, None)
        )
        args = await self._worker._convert_args(job.input, arg_types)

        self._loop.create_task(
            self._inbound.handle_signal(
                HandleSignalInput(name=job.signal_name, args=args)
            )
        )

    async def _start_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.StartWorkflow
    ) -> None:
        # Command builder call after workflow completes to run in this event
        # loop
        success: Optional[Any] = None
        workflow_failure: Optional[temporalio.exceptions.FailureError] = None
        task_failure: Optional[Exception] = None

        async def handle_complete() -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
            nonlocal success, workflow_failure, task_failure
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            if task_failure:
                raise task_failure
            elif workflow_failure:
                command.fail_workflow_execution.failure.SetInParent()
                try:
                    await temporalio.exceptions.apply_exception_to_failure(
                        workflow_failure,
                        self._worker._data_converter,
                        command.fail_workflow_execution.failure,
                    )
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting workflow exception"
                    ) from inner_err
            else:
                result_payloads = await self._worker._data_converter.encode([success])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command.complete_workflow_execution.result.CopyFrom(
                    temporalio.bridge.worker.to_bridge_payload(result_payloads[0])
                )
            return command

        # Async call to run on the scheduler thread
        async def run_workflow(input: ExecuteWorkflowInput) -> None:
            nonlocal success, workflow_failure, task_failure
            try:
                success = await self._inbound.execute_workflow(input)
            except temporalio.exceptions.FailureError as err:
                logger.debug(
                    f"Workflow raised failure with run ID {self._info.run_id}",
                    exc_info=True,
                )
                workflow_failure = err
            except Exception as err:
                task_failure = err
            self._pending_commands.append(handle_complete)

        # Schedule it
        arg_types, _ = self._worker._type_lookup.get_type_hints(self._defn.run_fn)
        input = ExecuteWorkflowInput(
            type=self._defn.cls,
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            run_fn=cast(Callable[..., Awaitable[Any]], self._defn.run_fn),
            args=await self._worker._convert_args(job.arguments, arg_types),
        )
        self._loop.create_task(run_workflow(input))


class _WorkflowInboundImpl(WorkflowInboundInterceptor):
    def __init__(
        self,
        running: _RunningWorkflow,
    ) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._running = running

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self._outbound = outbound

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        args = [self._running._instance] + list(input.args)
        return await input.run_fn(*args)

    async def handle_signal(self, input: HandleSignalInput) -> None:
        # Get the definition or fall through to dynamic
        signal_defn = self._running._signals.get(
            input.name
        ) or self._running._signals.get(None)
        # Presence of the signal handler was already checked, but they could
        # change the name so we re-check
        if not signal_defn:
            raise RuntimeError(
                f"signal handler for {input.name} expected but not found"
            )
        # Put self as arg first, then name only if dynamic
        args: Iterable[Any]
        if signal_defn.name:
            args = [self._running._instance] + list(input.args)
        else:
            args = [self._running._instance, input.name] + list(input.args)
        if inspect.iscoroutinefunction(signal_defn.fn):
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            signal_fn = cast(Callable[..., Awaitable[None]], signal_defn.fn)
            await signal_fn(*args)
        else:
            signal_fn = cast(Callable, signal_defn.fn)
            signal_fn(*args)

    async def handle_query(self, input: HandleQueryInput) -> Any:
        # Get the definition or fall through to dynamic
        query_defn = self._running._queries.get(
            input.name
        ) or self._running._queries.get(None)
        if not query_defn:
            raise RuntimeError(f"Workflow did not register a handler for {input.name}")
        # Put self as arg first, then name only if dynamic
        args: Iterable[Any]
        if query_defn.name:
            args = [self._running._instance] + list(input.args)
        else:
            args = [self._running._instance, input.name] + list(input.args)
        if inspect.iscoroutinefunction(query_defn.fn):
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            query_fn = cast(Callable[..., Awaitable[None]], query_defn.fn)
            return await query_fn(*args)
        else:
            query_fn = cast(Callable, query_defn.fn)
            return query_fn(*args)


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, workflow: _RunningWorkflow) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._workflow = workflow

    def info(self) -> temporalio.workflow.Info:
        return self._workflow._info

    def start_activity(
        self, input: StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        return self._workflow._loop.schedule_activity(input)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        return await self._workflow._loop.start_child_workflow(input)


class _WorkflowRuntimeImpl(temporalio.workflow._Runtime):
    def __init__(
        self, workflow: _RunningWorkflow, outbound: WorkflowOutboundInterceptor
    ) -> None:
        super().__init__()
        self._workflow = workflow
        self._outbound = outbound

    def info(self) -> temporalio.workflow.Info:
        return self._outbound.info()

    def now(self) -> datetime:
        return datetime.utcfromtimestamp(asyncio.get_running_loop().time())

    def start_activity(
        self,
        activity: Any,
        *args: Any,
        activity_id: Optional[str],
        task_queue: Optional[str],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        heartbeat_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cancellation_type: temporalio.workflow.ActivityCancellationType,
    ) -> temporalio.workflow.ActivityHandle[Any]:
        # Get activity definition if it's callable
        name: str
        arg_types: Optional[List[Type]] = None
        ret_type: Optional[Type] = None
        if isinstance(activity, str):
            name = activity
        elif callable(activity):
            defn = temporalio.activity._Definition.must_from_callable(activity)
            name = defn.name
            arg_types, ret_type = self._workflow._worker._type_lookup.get_type_hints(
                activity
            )
        else:
            raise TypeError("Activity must be a string or callable")

        return self._outbound.start_activity(
            StartActivityInput(
                activity=name,
                args=args,
                activity_id=activity_id,
                task_queue=task_queue,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                cancellation_type=cancellation_type,
                arg_types=arg_types,
                ret_type=ret_type,
            )
        )

    async def start_child_workflow(
        self,
        workflow: Any,
        *args: Any,
        id: str,
        task_queue: Optional[str],
        namespace: Optional[str],
        cancellation_type: temporalio.workflow.ChildWorkflowCancellationType,
        parent_close_policy: temporalio.workflow.ParentClosePolicy,
        execution_timeout: Optional[timedelta],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy,
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cron_schedule: str,
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[Mapping[str, Any]],
    ) -> temporalio.workflow.ChildWorkflowHandle[Any, Any]:
        # Use definition if callable
        name: str
        arg_types: Optional[List[Type]] = None
        ret_type: Optional[Type] = None
        if isinstance(workflow, str):
            name = workflow
        elif callable(workflow):
            defn = temporalio.workflow._Definition.must_from_run_fn(workflow)
            name = defn.name
            arg_types, ret_type = self._workflow._worker._type_lookup.get_type_hints(
                defn.run_fn
            )
        else:
            raise TypeError("Workflow must be a string or callable")

        return await self._outbound.start_child_workflow(
            StartChildWorkflowInput(
                workflow=name,
                args=args,
                id=id,
                task_queue=task_queue,
                namespace=namespace,
                cancellation_type=cancellation_type,
                parent_close_policy=parent_close_policy,
                execution_timeout=execution_timeout,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                id_reuse_policy=id_reuse_policy,
                retry_policy=retry_policy,
                cron_schedule=cron_schedule,
                memo=memo,
                search_attributes=search_attributes,
                arg_types=arg_types,
                ret_type=ret_type,
            )
        )

    async def wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        return await self._workflow._loop.wait_condition(fn, timeout=timeout)


_T = TypeVar("_T")
_Context: TypeAlias = Dict[str, Any]
_ExceptionHandler: TypeAlias = Callable[[asyncio.AbstractEventLoop, _Context], Any]


class _EventLoop(asyncio.AbstractEventLoop):
    def __init__(self, workflow: _RunningWorkflow) -> None:
        super().__init__()
        self._workflow = workflow
        self._time = 0.0
        self._ready: Deque[asyncio.Handle] = collections.deque()
        self._conditions: List[Tuple[Callable[[], bool], asyncio.Future]] = []
        # Keyed by seq
        self._pending_timers: Dict[int, asyncio.Handle] = {}
        self._pending_activities: Dict[int, _ActivityHandle] = {}
        self._pending_child_workflows: Dict[int, _ChildWorkflowHandle] = {}
        # Keyed by type
        self._curr_seqs: Dict[str, int] = {}
        self._exception_handler: Optional[_ExceptionHandler] = None

    def _next_seq(self, type: str) -> int:
        seq = self._curr_seqs.get(type, 0) + 1
        self._curr_seqs[type] = seq
        return seq

    # Returns true
    def _check_condition(self, fn: Callable[[], bool], fut: asyncio.Future) -> bool:
        if fn():
            fut.set_result(True)
            return True
        return False

    def resolve_activity_failure(
        self, seq: int, exc: temporalio.exceptions.FailureError
    ) -> None:
        handle = self._pending_activities.pop(seq, None)
        if not handle:
            raise RuntimeError(f"Failed finding activity handle for sequence {seq}")
        handle.result_fut.set_exception(exc)

    def resolve_activity_success(self, seq: int, result: Any) -> None:
        handle = self._pending_activities.pop(seq, None)
        if not handle:
            raise RuntimeError(f"Failed finding activity handle for sequence {seq}")
        handle.result_fut.set_result(result)

    def resolve_child_workflow_failure(
        self, seq: int, exc: temporalio.exceptions.FailureError
    ) -> None:
        handle = self._pending_child_workflows.pop(seq, None)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {seq}"
            )
        handle.result_fut.set_exception(exc)

    def resolve_child_workflow_start_failure(
        self, seq: int, exc: temporalio.exceptions.TemporalError
    ) -> None:
        handle = self._pending_child_workflows.pop(seq, None)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {seq}"
            )
        handle.start_fut.set_exception(exc)

    def resolve_child_workflow_start_success(self, seq: int, run_id: str) -> None:
        handle = self._pending_child_workflows.get(seq)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {seq}"
            )
        handle.original_run_id = run_id
        handle.start_fut.set_result(None)

    def resolve_child_workflow_success(self, seq: int, result: Any) -> None:
        handle = self._pending_child_workflows.pop(seq, None)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {seq}"
            )
        handle.result_fut.set_result(result)

    def resolve_timer(self, seq: int) -> None:
        handle = self._pending_timers.pop(seq, None)
        if not handle:
            raise RuntimeError(f"Failed finding timer handle for sequence {seq}")
        # Mark cancelled so the cancel() from things like asyncio.sleep() don't
        # invoke _timer_handle_cancelled
        handle._cancelled = True
        self._ready.append(handle)

    def run_once(self) -> None:
        try:
            asyncio._set_running_loop(self)

            # Run while there is anything ready
            while self._ready:

                # Run and remove all ready ones
                while self._ready:
                    handle = self._ready.popleft()
                    handle._run()

                # Check conditions which may add to the ready list
                self._conditions[:] = [
                    t for t in self._conditions if not self._check_condition(t[0], t[1])
                ]
        finally:
            asyncio._set_running_loop(None)

    def set_time(self, time: float) -> None:
        self._time = time

    def schedule_activity(self, input: StartActivityInput) -> _ActivityHandle:
        # Validate
        if not input.start_to_close_timeout and not input.schedule_to_close_timeout:
            raise ValueError(
                "Activity must have start_to_close_timeout or schedule_to_close_timeout"
            )

        seq = self._next_seq("activity")
        # We build the command in a callback to run on the other event loop
        async def build_command() -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            v = command.schedule_activity
            v.seq = seq
            v.activity_id = input.activity_id or str(seq)
            v.activity_type = input.activity
            v.namespace = self._workflow._worker._namespace
            v.task_queue = input.task_queue or self._workflow._info.task_queue
            # TODO(cretz): Headers
            # v.headers = input.he
            if input.args:
                v.arguments.extend(
                    temporalio.bridge.worker.to_bridge_payloads(
                        await self._workflow._worker._data_converter.encode(input.args)
                    )
                )
            if input.schedule_to_close_timeout:
                v.schedule_to_close_timeout.FromTimedelta(
                    input.schedule_to_close_timeout
                )
            if input.schedule_to_start_timeout:
                v.schedule_to_start_timeout.FromTimedelta(
                    input.schedule_to_start_timeout
                )
            if input.start_to_close_timeout:
                v.start_to_close_timeout.FromTimedelta(input.start_to_close_timeout)
            if input.heartbeat_timeout:
                v.heartbeat_timeout.FromTimedelta(input.heartbeat_timeout)
            if input.retry_policy:
                temporalio.bridge.worker.retry_policy_to_proto(
                    input.retry_policy, v.retry_policy
                )
            v.cancellation_type = cast(
                "temporalio.bridge.proto.workflow_commands.ActivityCancellationType.ValueType",
                int(input.cancellation_type),
            )
            return command

        # Create the handle and set as pending
        handle = _ActivityHandle(self.create_future(), input.arg_types, input.ret_type)
        self._pending_activities[seq] = handle
        self._workflow._pending_commands.append(build_command)
        return handle

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> _ChildWorkflowHandle:
        seq = self._next_seq("child_workflow")
        # We build the command in a callback to run on the other event loop
        async def build_command() -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            v = command.start_child_workflow_execution
            v.seq = seq
            v.namespace = input.namespace or self._workflow._worker._namespace
            v.workflow_id = input.id
            v.workflow_type = input.workflow
            v.task_queue = input.task_queue or self._workflow._info.task_queue
            if input.args:
                v.input.extend(
                    temporalio.bridge.worker.to_bridge_payloads(
                        await self._workflow._worker._data_converter.encode(input.args)
                    )
                )
            if input.execution_timeout:
                v.workflow_execution_timeout.FromTimedelta(input.execution_timeout)
            if input.run_timeout:
                v.workflow_run_timeout.FromTimedelta(input.run_timeout)
            if input.task_timeout:
                v.workflow_task_timeout.FromTimedelta(input.task_timeout)
            v.parent_close_policy = cast(
                "temporalio.bridge.proto.child_workflow.ParentClosePolicy.ValueType",
                int(input.parent_close_policy),
            )
            v.workflow_id_reuse_policy = cast(
                "temporalio.bridge.proto.common.WorkflowIdReusePolicy.ValueType",
                int(input.id_reuse_policy),
            )
            if input.retry_policy:
                temporalio.bridge.worker.retry_policy_to_proto(
                    input.retry_policy, v.retry_policy
                )
            v.cron_schedule = input.cron_schedule
            # TODO(cretz): Headers
            # v.headers = input.he
            if input.memo:
                for k, val in input.memo.items():
                    v.memo[k] = temporalio.bridge.worker.to_bridge_payload(
                        (await self._workflow._worker._data_converter.encode([val]))[0]
                    )
            if input.search_attributes:
                for k, val in input.search_attributes.items():
                    v.search_attributes[k] = temporalio.bridge.worker.to_bridge_payload(
                        # We have to use the default data converter for this
                        (await temporalio.converter.default().encode([val]))[0]
                    )
            v.cancellation_type = cast(
                "temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.ValueType",
                int(input.cancellation_type),
            )
            return command

        # Create the handle and set as pending
        handle = _ChildWorkflowHandle(
            input.id,
            self.create_future(),
            self.create_future(),
            input.arg_types,
            input.ret_type,
        )
        self._pending_child_workflows[seq] = handle
        self._workflow._pending_commands.append(build_command)

        # Wait on start
        await handle.start_fut
        return handle

    async def wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        fut = self.create_future()
        self._conditions.append((fn, fut))
        # TODO(cretz): Wrap timeout in cancellation scope
        await asyncio.wait_for(fut, timeout)

    ### Call overrides

    def _timer_handle_cancelled(self, handle: asyncio.TimerHandle) -> None:
        raise NotImplementedError

    def call_soon(
        self,
        callback: Callable[..., Any],
        *args: Any,
        context: Optional[contextvars.Context] = None,
    ) -> asyncio.Handle:
        handle = asyncio.Handle(callback, args, self, context)
        self._ready.append(handle)
        return handle

    def call_later(
        self,
        delay: float,
        callback: Callable[..., Any],
        *args: Any,
        context: Optional[contextvars.Context] = None,
    ) -> asyncio.TimerHandle:
        # Delay must be positive
        if delay < 0:
            raise RuntimeError("Attempting to schedule timer with negative delay")

        # Schedule a timer
        seq = self._next_seq("timer")
        command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
        command.start_timer.seq = seq
        command.start_timer.start_to_fire_timeout.FromNanoseconds(int(delay * 1e9))
        self._workflow._pending_commands.append(command)

        # Create and return the handle
        handle = asyncio.TimerHandle(self._time + delay, callback, args, self, context)
        self._pending_timers[seq] = handle
        return handle

    def time(self) -> float:
        return self._time

    def create_future(self) -> asyncio.Future[Any]:
        return asyncio.Future(loop=self)

    def create_task(
        self,
        coro: Union[Awaitable[_T], Generator[Any, None, _T]],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[_T]:
        return asyncio.Task(coro, loop=self, name=name)

    def get_exception_handler(self) -> Optional[_ExceptionHandler]:
        return self._exception_handler

    def set_exception_handler(self, handler: Optional[_ExceptionHandler]) -> None:
        self._exception_handler = handler

    def default_exception_handler(self, context: _Context) -> None:
        # Copied and slightly modified from
        # asyncio.BaseEventLoop.default_exception_handler
        message = context.get("message")
        if not message:
            message = "Unhandled exception in event loop"

        exception = context.get("exception")
        exc_info: Any
        if exception is not None:
            exc_info = (type(exception), exception, exception.__traceback__)
        else:
            exc_info = False

        log_lines = [message]
        for key in sorted(context):
            if key in {"message", "exception"}:
                continue
            value = context[key]
            if key == "source_traceback":
                tb = "".join(traceback.format_list(value))
                value = "Object created at (most recent call last):\n"
                value += tb.rstrip()
            elif key == "handle_traceback":
                tb = "".join(traceback.format_list(value))
                value = "Handle created at (most recent call last):\n"
                value += tb.rstrip()
            else:
                value = repr(value)
            log_lines.append(f"{key}: {value}")

        logger.error("\n".join(log_lines), exc_info=exc_info)

    def call_exception_handler(self, context: _Context) -> None:
        # Copied and slightly modified from
        # asyncio.BaseEventLoop.call_exception_handler
        if self._exception_handler is None:
            try:
                self.default_exception_handler(context)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException:
                # Second protection layer for unexpected errors
                # in the default implementation, as well as for subclassed
                # event loops with overloaded "default_exception_handler".
                logger.error("Exception in default exception handler", exc_info=True)
        else:
            try:
                self._exception_handler(self, context)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                # Exception in the user set custom exception handler.
                try:
                    # Let's try default handler.
                    self.default_exception_handler(
                        {
                            "message": "Unhandled error in exception handler",
                            "exception": exc,
                            "context": context,
                        }
                    )
                except (SystemExit, KeyboardInterrupt):
                    raise
                except BaseException:
                    # Guard 'default_exception_handler' in case it is
                    # overloaded.
                    logger.error(
                        "Exception in default exception handler "
                        "while handling an unexpected error "
                        "in custom exception handler",
                        exc_info=True,
                    )

    def get_debug(self) -> bool:
        return False


class _ActivityHandle(temporalio.workflow.ActivityHandle[Any]):
    def __init__(
        self,
        result_fut: asyncio.Future[Any],
        arg_types: Optional[List[Type]],
        ret_type: Optional[Type],
    ) -> None:
        super().__init__()
        self.result_fut = result_fut
        self.arg_types = arg_types
        self.ret_type = ret_type

    async def result(self) -> Any:
        return await self.result_fut

    def cancel(self) -> None:
        raise NotImplementedError


class _ChildWorkflowHandle(temporalio.workflow.ChildWorkflowHandle[Any, Any]):
    def __init__(
        self,
        id: str,
        start_fut: asyncio.Future[None],
        result_fut: asyncio.Future[Any],
        arg_types: Optional[List[Type]],
        ret_type: Optional[Type],
    ) -> None:
        super().__init__()
        self._id = id
        self._original_run_id = "<unknown>"
        self.start_fut = start_fut
        self.result_fut = result_fut
        self.arg_types = arg_types
        self.ret_type = ret_type

    @property
    def id(self) -> str:
        return self._id

    @property
    def original_run_id(self) -> Optional[str]:
        return self._original_run_id

    @original_run_id.setter
    def original_run_id(self, value: str) -> None:
        self._original_run_id = value

    async def signal(self, signal: Union[str, Callable], *args: Any) -> None:
        raise NotImplementedError

    async def result(self) -> Any:
        return await self.result_fut

    def cancel(self) -> None:
        raise NotImplementedError


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> datetime:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)
