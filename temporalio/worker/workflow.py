from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Type,
    cast,
)

import google.protobuf.timestamp_pb2

import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
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
        type_hint_eval_str: bool,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Iterable[Interceptor],
        # TODO(cretz): max_threads: int = 1,
    ) -> None:
        self._bridge_worker = bridge_worker
        self._namespace = namespace
        self._task_queue = task_queue
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
        while True:
            try:
                act = await self._bridge_worker().poll_workflow_activation()
                await self._handle_activation(act)
            except temporalio.bridge.worker.PollShutdownError:
                return
            except Exception:
                # Should never happen
                logger.exception(f"Workflow runner failed")

    async def _handle_activation(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> None:
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

            # Handle activation
            await workflow.on_activation(act)

            # If the last job is to remove from cache, do so
            if act.jobs[-1].HasField("remove_from_cache"):
                del self._running_workflows[act.run_id]
        except:
            logger.exception(f"Failed activation on workflow with run ID {act.run_id}")
            # TODO(cretz): Complete activation as failure

    async def _handle_completion(
        self,
        comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    ) -> None:
        # Send the completion
        try:
            await self._bridge_worker().complete_workflow_activation(comp)
        except:
            # All we can really do here is log
            logger.exception(f"Failed sending completion with run ID {comp.run_id}")


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

        # Init the interceptor
        impl: WorkflowInboundInterceptor = _WorkflowInboundImpl(worker, defn)
        for interceptor in reversed(list(worker._interceptors)):
            impl = interceptor.intercept_workflow(impl)
        impl.init(_WorkflowOutboundImpl(worker, info))

        return _RunningWorkflow(worker, defn, impl)

    def __init__(
        self,
        worker: _WorkflowWorker,
        defn: _TypedDefinition,
        inbound: WorkflowInboundInterceptor,
    ) -> None:
        self._worker = worker
        self._defn = defn
        self._inbound = inbound
        self._scheduler = _Scheduler()
        self._primary_task: Optional[asyncio.Task] = None

    async def on_activation(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> None:
        for job in act.jobs:
            if job.HasField("start_workflow"):
                if self._primary_task:
                    raise RuntimeError(
                        "Received start workflow while task already present"
                    )
                # Convert arguments (only use type hints if they match count)
                start = job.start_workflow
                arg_types = self._defn.arg_types
                if arg_types and len(arg_types) != len(start.arguments):
                    arg_types = None
                try:
                    args = (
                        []
                        if not start.arguments
                        else await self._worker._data_converter.decode(
                            temporalio.bridge.worker.from_bridge_payloads(
                                start.arguments
                            ),
                            type_hints=arg_types,
                        )
                    )
                except Exception as err:
                    raise temporalio.exceptions.ApplicationError(
                        "Failed decoding arguments"
                    ) from err

                # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
                run_fn = cast(Callable[..., Awaitable[Any]], self._defn.defn.run_fn)
                input = ExecuteWorkflowInput(
                    type=self._defn.defn.cls,
                    run_fn=run_fn,
                    args=args,
                )
                self._primary_task = self._scheduler.add_task(
                    self._inbound.execute_workflow(input)
                )
            else:
                raise RuntimeError(f"unrecognized job: {job.WhichOneof('variant')}")


class _WorkflowInboundImpl(WorkflowInboundInterceptor):
    def __init__(
        self,
        worker: _WorkflowWorker,
        defn: _TypedDefinition,
    ) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._defn = defn

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        # Set the context callables. We are setting values instead of replacing
        # the context just in case other interceptors held a reference.
        context = temporalio.workflow._Context.current()
        context.info = outbound.info

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        raise NotImplementedError()


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, worker: _WorkflowWorker, info: temporalio.workflow.Info) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._worker = worker
        self._info = info

    def info(self) -> temporalio.workflow.Info:
        return self._info


class _Scheduler:
    _loop: asyncio.AbstractEventLoop
    _waiters: Deque[asyncio.Future]

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        # Set of tasks waiting on a single wakeup (modeled off asyncio.Event)
        self._waiters = collections.deque()
        # Put ourself on the loop
        setattr(self._loop, "__temporal_scheduler", self)

    @staticmethod
    def get_running_scheduler() -> _Scheduler:
        scheduler: Optional[_Scheduler] = getattr(
            asyncio.get_running_loop(), "__temporal_scheduler", None
        )
        if scheduler is None:
            raise RuntimeError("no scheduler in this event loop")
        return scheduler

    def add_task(self, task: Awaitable) -> asyncio.Task:
        return asyncio.ensure_future(task, loop=self._loop)

    async def wait(self) -> None:
        # Mark current task as having reached our wait
        curr_task = asyncio.current_task(self._loop)
        if curr_task is None:
            raise RuntimeError("no current task or not in current event loop")
        setattr(curr_task, "__temporal_waiting", True)

        # Create future and wait on it
        fut = self._loop.create_future()
        self._waiters.append(fut)
        try:
            await fut
            return
        finally:
            # Unset that the task is waiting and remove from waiters
            setattr(curr_task, "__temporal_waiting", False)
            self._waiters.remove(fut)

    def tick(self) -> None:
        # Go over every waiter and set it as done
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(True)

        # Run one iteration
        # Ref https://stackoverflow.com/questions/29782377/is-it-possible-to-run-only-a-single-step-of-the-asyncio-event-loop
        self._loop.call_soon(self._loop.stop)
        self._loop.run_forever()

        # Make sure every task is done or waiting on our future
        for task in asyncio.all_tasks(self._loop):
            if not getattr(task, "__temporal_waiting", False):
                raise RuntimeError(
                    "Task did not complete and is not waiting on the Temporal scheduler"
                )


def _proto_to_datetime(
    ts: google.protobuf.timestamp_pb2.Timestamp,
) -> datetime:
    # Protobuf doesn't set the timezone but we want to
    return ts.ToDatetime().replace(tzinfo=timezone.utc)
