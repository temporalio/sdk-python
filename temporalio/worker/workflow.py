from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Type,
    TypeVar,
    cast,
)

import google.protobuf.timestamp_pb2

import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.worker
import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow
import temporalio.workflow_service

from .interceptor import (
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    Interceptor,
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
        type_hint_eval_str: bool,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Iterable[Interceptor],
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
        self._running_workflows: Dict[str, _RunningWorkflow] = {}

        # Validate and build workflow dict
        self._workflows: Dict[str, _TypedDefinition] = {}
        for workflow in workflows:
            defn = _TypedDefinition.from_workflow_class(
                workflow, type_hint_eval_str=type_hint_eval_str
            )
            self._workflows[defn.defn.name] = defn

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

        # Send off completion (intentionally let any exception bubble out)
        if LOG_PROTOS:
            logger.debug("Sending completion: %s", completion)
        await self._bridge_worker().complete_workflow_activation(completion)

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


@dataclass(frozen=True)
class _TypedDefinition:
    defn: temporalio.workflow._Definition
    arg_types: Optional[List[Type]]
    # Only present for non-dynamic signals/queries with type args
    signal_arg_types: Mapping[str, List[Type]]
    query_arg_types: Mapping[str, List[Type]]

    @staticmethod
    def from_workflow_class(cls: Type, *, type_hint_eval_str: bool) -> _TypedDefinition:
        defn = temporalio.workflow._Definition.from_class(cls)
        if not defn:
            cls_name = getattr(cls, "__name__", "<unknown>")
            raise ValueError(
                f"Workflow {cls_name} missing attributes, was it decorated with @workflow.defn?"
            )
        # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
        run_fn = cast(Callable[..., Any], defn.run_fn)
        arg_types, _ = temporalio.converter._type_hints_from_func(
            run_fn, eval_str=type_hint_eval_str
        )
        signal_arg_types = {}
        for name, signal_defn in defn.signals.items():
            if name:
                # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
                signal_fn = cast(Callable[..., Any], signal_defn.fn)
                arg_types, _ = temporalio.converter._type_hints_from_func(
                    signal_fn, eval_str=type_hint_eval_str
                )
                if arg_types:
                    signal_arg_types[name] = arg_types
        query_arg_types = {}
        for name, query_defn in defn.queries.items():
            if name:
                arg_types, _ = temporalio.converter._type_hints_from_func(
                    query_defn.fn, eval_str=type_hint_eval_str
                )
                if arg_types:
                    query_arg_types[name] = arg_types
        return _TypedDefinition(
            defn=defn,
            arg_types=arg_types,
            signal_arg_types=signal_arg_types,
            query_arg_types=query_arg_types,
        )


class _RunningWorkflow:
    @staticmethod
    def create(
        worker: _WorkflowWorker,
        defn: _TypedDefinition,
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

        # Set the context early so the logging adapter works and interceptors
        # have it
        temporalio.workflow._Context.set(
            temporalio.workflow._Context(info=lambda: info)
        )
        temporalio.workflow.logger.debug("Starting workflow")

        # Create the running workflow
        instance = defn.defn.cls()
        return _RunningWorkflow(worker, defn, info, instance)

    def __init__(
        self,
        worker: _WorkflowWorker,
        defn: _TypedDefinition,
        info: temporalio.workflow.Info,
        instance: Any,
    ) -> None:
        self._worker = worker
        self._defn = defn
        self._info = info
        self._instance = instance
        self._scheduler = _Scheduler()
        self._pending_commands: List[
            Callable[
                [], Awaitable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]
            ]
        ] = []
        self._pending_activation_failure: Optional[Exception] = None

        # We maintain signals and queries on this class since handlers can be
        # added during workflow execution
        self._signals = dict(defn.defn.signals)
        self._signal_arg_types = dict(defn.signal_arg_types)
        self._queries = dict(defn.defn.queries)
        self._query_arg_types = dict(defn.query_arg_types)

        # Maintain buffered signals for later-added dynamic handlers
        self._buffered_signals: Dict[
            str, List[temporalio.bridge.proto.workflow_activation.SignalWorkflow]
        ] = {}

        # Init the interceptor
        self._inbound: WorkflowInboundInterceptor = _WorkflowInboundImpl(self)
        for interceptor in reversed(list(worker._interceptors)):
            self._inbound = interceptor.intercept_workflow(self._inbound)
        self._inbound.init(_WorkflowOutboundImpl(worker, info))

    async def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> Iterable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]:
        global DEADLOCK_TIMEOUT_SECONDS

        self._pending_commands = []
        self._pending_activation_failure = None

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
                if job.HasField("query_workflow"):
                    await self._query_workflow(job.query_workflow)
                elif job.HasField("remove_from_cache"):
                    # Ignore, handled by _handle_activation
                    pass
                elif job.HasField("signal_workflow"):
                    await self._signal_workflow(job.signal_workflow)
                elif job.HasField("start_workflow"):
                    await self._start_workflow(job.start_workflow)
                else:
                    print(f"TODO(cretz) JOB: {job.WhichOneof('variant')}")
                    # raise RuntimeError(f"unrecognized job: {job.WhichOneof('variant')}")

            # Run the scheduler on a thread until blocked. We will throw a
            # deadlock detected error if it exceeds the timeout
            exec_task = asyncio.get_running_loop().run_in_executor(
                self._worker._workflow_task_executor,
                self._scheduler.run_until_all_blocked,
            )
            try:
                await asyncio.wait_for(exec_task, DEADLOCK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Potential deadlock detected, workflow didn't yield within {DEADLOCK_TIMEOUT_SECONDS} second(s)"
                )

            # If there's a pending failure, we have to raise it so it can fail the
            # activation
            if self._pending_activation_failure:
                raise self._pending_activation_failure

        # Apply all collected commands
        commands: List[temporalio.bridge.proto.workflow_commands.WorkflowCommand] = []
        for pending_command in self._pending_commands:
            commands.append(await pending_command())
        return commands

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
        arg_types = self._query_arg_types.get(job.query_type)
        args = await self._worker._convert_args(job.arguments, arg_types)

        # Schedule it
        input = HandleQueryInput(
            id=job.query_id,
            name=job.query_type,
            args=args,
        )
        self._scheduler.add_task(run_query(input))

    async def _signal_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.SignalWorkflow
    ) -> None:
        # Find the handler or fall through to the dynamic signal handler if
        # available. We only do this lookup to know the arg types and to buffer
        # if necessary. The interceptor will end up performing this lookup
        # again.
        signal_def = self._signals.get(job.signal_name) or self._signals.get(None)
        # If there is no definition, we buffer and ignore
        if not signal_def:
            self._buffered_signals.setdefault(job.signal_name, []).append(job)
            return

        # Add the interceptor invocation to the scheduler
        arg_types = (
            self._signal_arg_types.get(signal_def.name) if signal_def.name else None
        )
        args = await self._worker._convert_args(job.input, arg_types)
        self._scheduler.add_task(
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
        input = ExecuteWorkflowInput(
            type=self._defn.defn.cls,
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            run_fn=cast(Callable[..., Awaitable[Any]], self._defn.defn.run_fn),
            args=await self._worker._convert_args(job.arguments, self._defn.arg_types),
        )
        self._scheduler.add_task(run_workflow(input))


class _WorkflowInboundImpl(WorkflowInboundInterceptor):
    def __init__(
        self,
        running: _RunningWorkflow,
    ) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._running = running

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        # Set the context callables. We are setting values instead of replacing
        # the context just in case other interceptors held a reference.
        context = temporalio.workflow._Context.current()
        context.info = outbound.info

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
        # Put self as arg first
        args = [self._running._instance] + list(input.args)
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
        # Put self as arg first
        args = [self._running._instance] + list(input.args)
        if inspect.iscoroutinefunction(query_defn.fn):
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            query_fn = cast(Callable[..., Awaitable[None]], query_defn.fn)
            return await query_fn(*args)
        else:
            query_fn = cast(Callable, query_defn.fn)
            return query_fn(*args)


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, worker: _WorkflowWorker, info: temporalio.workflow.Info) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._info = info

    def info(self) -> temporalio.workflow.Info:
        return self._info


_TaskReturn = TypeVar("_TaskReturn")


class _Scheduler:
    _loop: asyncio.AbstractEventLoop
    # _waiters: Deque[asyncio.Future]

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        # Set of tasks waiting on a single wakeup (modeled off asyncio.Event)
        # self._waiters = collections.deque()
        # Put ourself on the loop
        setattr(self._loop, "__temporal_scheduler", self)

    # @staticmethod
    # def get_running_scheduler() -> _Scheduler:
    #     scheduler: Optional[_Scheduler] = getattr(
    #         asyncio.get_running_loop(), "__temporal_scheduler", None
    #     )
    #     if scheduler is None:
    #         raise RuntimeError("no scheduler in this event loop")
    #     return scheduler

    def add_task(self, task: Awaitable[_TaskReturn]) -> asyncio.Task[_TaskReturn]:
        return asyncio.ensure_future(task, loop=self._loop)

    # async def wait(self) -> None:
    #     # Mark current task as having reached our wait
    #     curr_task = asyncio.current_task(self._loop)
    #     if curr_task is None:
    #         raise RuntimeError("no current task or not in current event loop")
    #     setattr(curr_task, "__temporal_waiting", True)

    #     # Create future and wait on it
    #     fut = self._loop.create_future()
    #     self._waiters.append(fut)
    #     try:
    #         await fut
    #         return
    #     finally:
    #         # Unset that the task is waiting and remove from waiters
    #         setattr(curr_task, "__temporal_waiting", False)
    #         self._waiters.remove(fut)

    # def tick(self) -> None:
    #     # Go over every waiter and set it as done
    #     for fut in self._waiters:
    #         if not fut.done():
    #             fut.set_result(True)

    #     # Run one iteration
    #     # Ref https://stackoverflow.com/questions/29782377/is-it-possible-to-run-only-a-single-step-of-the-asyncio-event-loop
    #     self._loop.call_soon(self._loop.stop)
    #     self._loop.run_forever()

    #     # Make sure every task is done or waiting on our future
    #     for task in asyncio.all_tasks(self._loop):
    #         if not getattr(task, "__temporal_waiting", False):
    #             raise RuntimeError(
    #                 "Task did not complete and is not waiting on the Temporal scheduler"
    #             )

    def run_until_all_blocked(self) -> None:
        # Run one iteration
        # Ref https://stackoverflow.com/questions/29782377/is-it-possible-to-run-only-a-single-step-of-the-asyncio-event-loop
        self._loop.call_soon(self._loop.stop)
        self._loop.run_forever()

        # TODO(cretz): Repeatedly run until all tasks are waiting on us properly
        # tasks = asyncio.all_tasks(self._loop)
        # print("TASK COUNT", len(tasks))
        # for task in tasks:
        #     print("  TASK DONE: ", task.done())


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> datetime:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)
