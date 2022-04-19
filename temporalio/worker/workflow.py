from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Type, cast

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
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)

logger = logging.getLogger(__name__)


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
            defn = temporalio.workflow._Definition.from_class(workflow)
            if not defn:
                cls_name = getattr(workflow, "__name__", "<unknown>")
                raise ValueError(
                    f"Workflow {cls_name} missing attributes, was it decorated with @workflow.defn?"
                )
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            run_fn = cast(Callable[..., Any], defn.run_fn)
            arg_types, ret_type = temporalio.converter._type_hints_from_func(
                run_fn, eval_str=type_hint_eval_str
            )
            self._workflows[defn.name] = _TypedDefinition(
                defn=defn, arg_types=arg_types, ret_type=ret_type
            )

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

            # Perform activation in a separate thread and await it
            # TODO(cretz): Deadlock detection at the thread level
            commands = await asyncio.get_running_loop().run_in_executor(
                self._workflow_task_executor, workflow.activate, act
            )

            # Set successful command
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


@dataclass(frozen=True)
class _TypedDefinition:
    defn: temporalio.workflow._Definition
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


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
            temporalio.bridge.proto.workflow_commands.WorkflowCommand
        ] = []
        self._pending_activation_failure: Optional[Exception] = None

        # Init the interceptor
        self._inbound: WorkflowInboundInterceptor = _WorkflowInboundImpl(self)
        for interceptor in reversed(list(worker._interceptors)):
            self._inbound = interceptor.intercept_workflow(self._inbound)
        self._inbound.init(_WorkflowOutboundImpl(worker, info))

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> Iterable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]:
        self._pending_commands = []
        self._pending_activation_failure = None

        # Apply jobs
        for job in act.jobs:
            if job.HasField("remove_from_cache"):
                # Ignore, handled by _handle_activation
                pass
            elif job.HasField("start_workflow"):
                self._scheduler.add_task(self._start_workflow(job.start_workflow))
            else:
                print(f"TODO(cretz) JOB: {job.WhichOneof('variant')}")
                # raise RuntimeError(f"unrecognized job: {job.WhichOneof('variant')}")

        # Run the scheduler until blocked
        self._scheduler.run_until_all_blocked()

        # If there's a pending failure, we have to raise it so it can fail the
        # activation
        if self._pending_activation_failure:
            raise self._pending_activation_failure
        return self._pending_commands

    async def _start_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.StartWorkflow
    ) -> None:
        try:
            # Convert arguments (only use type hints if they match count)
            arg_types = self._defn.arg_types
            if arg_types and len(arg_types) != len(job.arguments):
                arg_types = None
            try:
                args = (
                    []
                    if not job.arguments
                    else await self._worker._data_converter.decode(
                        temporalio.bridge.worker.from_bridge_payloads(job.arguments),
                        type_hints=arg_types,
                    )
                )
            except Exception as err:
                raise RuntimeError("Failed decoding arguments") from err

            # Execute and capture ApplicationError as failed workflow
            try:
                result = await self._inbound.execute_workflow(
                    ExecuteWorkflowInput(
                        type=self._defn.defn.cls,
                        # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
                        run_fn=cast(
                            Callable[..., Awaitable[Any]], self._defn.defn.run_fn
                        ),
                        args=args,
                    )
                )
                result_payloads = await self._worker._data_converter.encode([result])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                self._pending_commands.append(
                    temporalio.bridge.proto.workflow_commands.WorkflowCommand(
                        complete_workflow_execution=temporalio.bridge.proto.workflow_commands.CompleteWorkflowExecution(
                            result=temporalio.bridge.worker.to_bridge_payload(
                                result_payloads[0]
                            ),
                        ),
                    )
                )
            except temporalio.exceptions.FailureError as err:
                # TODO(cretz): Confirm which exceptions are what kind of failures
                logger.debug(
                    f"Workflow raised failure with run ID {self._info.run_id}",
                    exc_info=True,
                )
                command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
                command.fail_workflow_execution.failure.SetInParent()
                try:
                    await temporalio.exceptions.apply_exception_to_failure(
                        err,
                        self._worker._data_converter,
                        command.fail_workflow_execution.failure,
                    )
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting application error"
                    ) from inner_err
                self._pending_commands.append(command)
        except Exception as fail_err:
            logger.warning(
                f"Workflow failed with run ID {self._info.run_id}", exc_info=True
            )
            self._pending_activation_failure = fail_err


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


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, worker: _WorkflowWorker, info: temporalio.workflow.Info) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._info = info

    def info(self) -> temporalio.workflow.Info:
        return self._info


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

    def add_task(self, task: Awaitable) -> asyncio.Task:
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


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> datetime:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)
