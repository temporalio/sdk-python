"""Workflow worker runner and instance."""

from __future__ import annotations

import asyncio
import collections
import contextvars
import inspect
import logging
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from typing_extensions import TypeAlias

import temporalio.activity
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.worker
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow

from .interceptor import (
    ContinueAsNewInput,
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)

logger = logging.getLogger(__name__)


class WorkflowRunner(ABC):
    """Abstract runner for workflows that creates workflow instances to run.

    :py:class:`UnsandboxedWorkflowRunner` is an implementation that locally runs
    the workflow.
    """

    @abstractmethod
    async def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        """Create a workflow instance that can handle activations.

        Args:
            det: Serializable details that can be used to create the instance.

        Returns:
            Workflow instance that can handle activations.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class WorkflowInstanceDetails:
    """Immutable, serializable details for creating a workflow instance."""

    payload_converter_class: Type[temporalio.converter.PayloadConverter]
    interceptor_classes: Iterable[Type[WorkflowInboundInterceptor]]
    defn: temporalio.workflow._Definition
    info: temporalio.workflow.Info
    type_hint_eval_str: bool


class WorkflowInstance(ABC):
    """Instance of a workflow that can handle activations."""

    @abstractmethod
    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> Iterable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]:
        """Handle an activation and return a list of resulting commands.

        Args:
            act: Protobuf activation.

        Returns:
            Set of protobuf commands.
        """
        raise NotImplementedError


class UnsandboxedWorkflowRunner(WorkflowRunner):
    """Workflow runner that does not do any sandboxing."""

    async def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        """Create an unsandboxed workflow instance."""
        # We ignore MyPy failing to instantiate this because it's not _really_
        # abstract at runtime. All of the asyncio.AbstractEventLoop calls that
        # we are not implementing are not abstract, they just throw not-impl'd
        # errors.
        return _WorkflowInstanceImpl(det)  # type: ignore[abstract]


_T = TypeVar("_T")
_Context: TypeAlias = Dict[str, Any]
_ExceptionHandler: TypeAlias = Callable[[asyncio.AbstractEventLoop, _Context], Any]


class _WorkflowInstanceImpl(
    WorkflowInstance, temporalio.workflow._Runtime, asyncio.AbstractEventLoop
):
    def __init__(self, det: WorkflowInstanceDetails) -> None:
        # No init for AbstractEventLoop
        WorkflowInstance.__init__(self)
        temporalio.workflow._Runtime.__init__(self)
        self._payload_converter = det.payload_converter_class()
        self._defn = det.defn
        self._info = det.info
        self._pending_commands: List[
            temporalio.bridge.proto.workflow_commands.WorkflowCommand
        ] = []
        self._primary_task: Optional[asyncio.Task[None]] = None
        self._time = 0.0
        # Handles which are ready to run on the next event loop iteration
        self._ready: Deque[asyncio.Handle] = collections.deque()
        self._conditions: List[Tuple[Callable[[], bool], asyncio.Future]] = []
        # Only set for continue-as-new
        self._force_stop_loop: bool = False
        # Keyed by seq
        self._pending_timers: Dict[int, _TimerHandle] = {}
        self._pending_activities: Dict[int, _ActivityHandle] = {}
        self._pending_child_workflows: Dict[int, _ChildWorkflowHandle] = {}
        # Keyed by type
        self._curr_seqs: Dict[str, int] = {}
        # TODO(cretz): Any concerns about not sharing this? Maybe the types I
        # need to lookup should be done at definition time?
        self._type_lookup = temporalio.converter._FunctionTypeLookup(
            det.type_hint_eval_str
        )
        self._exception_handler: Optional[_ExceptionHandler] = None
        # The actual instance, instantiated on first _run_once
        self._object: Any = None

        # We maintain signals and queries on this class since handlers can be
        # added during workflow execution
        self._signals = dict(self._defn.signals)
        self._queries = dict(self._defn.queries)

        # Maintain buffered signals for later-added dynamic handlers
        self._buffered_signals: Dict[
            str, List[temporalio.bridge.proto.workflow_activation.SignalWorkflow]
        ] = {}

        # Create interceptors. We do this with our runtime on the loop just in
        # case they want to access info() during init().
        temporalio.workflow._Runtime.set_on_loop(asyncio.get_running_loop(), self)
        try:
            root_inbound = _WorkflowInboundImpl(self)
            self._inbound: WorkflowInboundInterceptor = root_inbound
            for interceptor_class in reversed(list(det.interceptor_classes)):
                self._inbound = interceptor_class(self._inbound)
            # During init we set ourselves on the current loop
            self._inbound.init(_WorkflowOutboundImpl(self))
            self._outbound = root_inbound._outbound
        finally:
            # Remove our runtime from the loop
            temporalio.workflow._Runtime.set_on_loop(asyncio.get_running_loop(), None)

        # Set ourselves on our own loop
        temporalio.workflow._Runtime.set_on_loop(self, self)

    #### Activation functions ####
    # These are in alphabetical order and besides "activate", all other calls
    # are "_apply_" + the job field name.

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> Iterable[temporalio.bridge.proto.workflow_commands.WorkflowCommand]:
        # Reset pending commands and time
        self._pending_commands = []
        self._time = act.timestamp.ToMicroseconds() / 1e6

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

        # Apply every job set, running after each set
        for job_set in job_sets:
            if not job_set:
                continue
            for job in job_set:
                # Let errors bubble out of these to the caller to fail the task
                self._apply(job)

            # Run one iteration of the loop
            self._run_once()

        # Return pending commands
        return self._pending_commands

    def _apply(
        self, job: temporalio.bridge.proto.workflow_activation.WorkflowActivationJob
    ) -> None:
        if job.HasField("cancel_workflow"):
            self._apply_cancel_workflow(job.cancel_workflow)
        elif job.HasField("fire_timer"):
            self._apply_fire_timer(job.fire_timer)
        elif job.HasField("query_workflow"):
            self._apply_query_workflow(job.query_workflow)
        elif job.HasField("notify_has_patch"):
            # TODO(cretz): This
            pass
        elif job.HasField("remove_from_cache"):
            # Ignore, handled externally
            pass
        elif job.HasField("resolve_activity"):
            self._apply_resolve_activity(job.resolve_activity)
        elif job.HasField("resolve_child_workflow_execution"):
            self._apply_resolve_child_workflow_execution(
                job.resolve_child_workflow_execution
            )
        elif job.HasField("resolve_child_workflow_execution_start"):
            self._apply_resolve_child_workflow_execution_start(
                job.resolve_child_workflow_execution_start
            )
        elif job.HasField("resolve_request_cancel_external_workflow"):
            # TODO(cretz): This
            pass
        elif job.HasField("resolve_signal_external_workflow"):
            # TODO(cretz): This
            pass
        elif job.HasField("signal_workflow"):
            self._apply_signal_workflow(job.signal_workflow)
        elif job.HasField("start_workflow"):
            self._apply_start_workflow(job.start_workflow)
        elif job.HasField("update_random_seed"):
            # TODO(cretz): This
            pass
        else:
            raise RuntimeError(f"Unrecognized job: {job.WhichOneof('variant')}")

    def _apply_cancel_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.CancelWorkflow
    ) -> None:
        # TODO(cretz): Details or cancel message or whatever?
        if self._primary_task:
            self._primary_task.cancel()

    def _apply_fire_timer(
        self, job: temporalio.bridge.proto.workflow_activation.FireTimer
    ) -> None:
        handle = self._pending_timers.pop(job.seq, None)
        if not handle:
            raise RuntimeError(f"Failed finding timer handle for sequence {job.seq}")
        # Mark cancelled so the cancel() from things like asyncio.sleep() don't
        # invoke _timer_handle_cancelled
        handle._cancelled = True
        self._ready.append(handle)

    def _apply_query_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.QueryWorkflow
    ) -> None:
        # Async call to run on the scheduler thread
        async def run_query(input: HandleQueryInput) -> None:
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            command.respond_to_query.query_id = job.query_id
            try:
                success = await self._inbound.handle_query(input)
                result_payloads = self._payload_converter.to_payloads([success])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command.respond_to_query.succeeded.response.CopyFrom(
                    temporalio.bridge.worker.to_bridge_payload(result_payloads[0])
                )
                self._pending_commands.append(command)
            except Exception as err:
                try:
                    temporalio.exceptions.apply_exception_to_failure(
                        err,
                        self._payload_converter,
                        command.respond_to_query.failed,
                    )
                    self._pending_commands.append(command)
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting application error"
                    ) from inner_err

        # Just find the arg types for now. The interceptor will be responsible
        # for checking whether the query definition actually exists.
        query_defn = self._queries.get(job.query_type)
        arg_types, _ = (
            self._type_lookup.get_type_hints(query_defn.fn)
            if query_defn
            else (None, None)
        )
        args = self._convert_payloads(job.arguments, arg_types)

        # Schedule it
        self.create_task(
            run_query(
                HandleQueryInput(
                    id=job.query_id,
                    name=job.query_type,
                    args=args,
                )
            )
        )

    def _apply_resolve_activity(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveActivity
    ) -> None:
        handle = self._pending_activities.get(job.seq)
        if not handle:
            raise RuntimeError(f"Failed finding activity handle for sequence {job.seq}")
        if job.result.HasField("completed"):
            self._pending_activities.pop(job.seq)
            ret: Optional[Any] = None
            if job.result.completed.HasField("result"):
                ret_types = [handle._input.ret_type] if handle._input.ret_type else None
                ret_vals = self._convert_payloads(
                    [job.result.completed.result],
                    ret_types,
                )
                ret = ret_vals[0]
            handle._resolve_success(ret)
        elif job.result.HasField("failed"):
            self._pending_activities.pop(job.seq)
            handle._resolve_failure(
                temporalio.exceptions.failure_to_error(
                    job.result.failed.failure, self._payload_converter
                )
            )
        elif job.result.HasField("cancelled"):
            self._pending_activities.pop(job.seq)
            handle._resolve_failure(
                temporalio.exceptions.failure_to_error(
                    job.result.cancelled.failure, self._payload_converter
                )
            )
        elif job.result.HasField("backoff"):
            # Async call to wait for the timer then execute the activity again
            async def backoff_then_reschedule() -> None:
                # Don't pop, we want this to remain as pending
                handle = self._pending_activities[job.seq]
                try:
                    await asyncio.sleep(
                        job.result.backoff.backoff_duration.ToTimedelta().total_seconds()
                    )
                except Exception as err:
                    self._pending_activities.pop(job.seq)
                    handle._resolve_failure(err)
                    return
                self._pending_commands.append(
                    handle._build_schedule_command(job.result.backoff)
                )

            # Schedule it
            # TODO(cretz): Do we need to do this inside the activity task so it
            # can be cancelled?
            self.create_task(backoff_then_reschedule())
        else:
            raise RuntimeError("Activity did not have result")

    def _apply_resolve_child_workflow_execution(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveChildWorkflowExecution,
    ) -> None:
        # No matter the result, we know we want to pop
        handle = self._pending_child_workflows.pop(job.seq, None)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {job.seq}"
            )
        if job.result.HasField("completed"):
            ret: Optional[Any] = None
            if job.result.completed.HasField("result"):
                ret_types = [handle._input.ret_type] if handle._input.ret_type else None
                ret_vals = self._convert_payloads(
                    [job.result.completed.result],
                    ret_types,
                )
                ret = ret_vals[0]
            handle._resolve_success(ret)
        elif job.result.HasField("failed"):
            handle._resolve_failure(
                temporalio.exceptions.failure_to_error(
                    job.result.failed.failure, self._payload_converter
                )
            )
        elif job.result.HasField("cancelled"):
            handle._resolve_failure(
                temporalio.exceptions.failure_to_error(
                    job.result.cancelled.failure, self._payload_converter
                )
            )
        else:
            raise RuntimeError("Child workflow did not have result")

    def _apply_resolve_child_workflow_execution_start(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveChildWorkflowExecutionStart,
    ) -> None:
        handle = self._pending_child_workflows.get(job.seq)
        if not handle:
            raise RuntimeError(
                f"Failed finding child workflow handle for sequence {job.seq}"
            )
        if job.HasField("succeeded"):
            handle._resolve_start_success(job.succeeded.run_id)
        elif job.HasField("failed"):
            self._pending_child_workflows.pop(job.seq)
            if (
                job.failed.cause
                == temporalio.bridge.proto.child_workflow.StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS
            ):
                handle._resolve_failure(
                    temporalio.exceptions.WorkflowAlreadyStartedError(
                        job.failed.workflow_id, job.failed.workflow_type
                    )
                )
            else:
                handle._resolve_failure(
                    RuntimeError("Unknown child start fail cause: {job.failed.cause}")
                )
        elif job.HasField("cancelled"):
            self._pending_child_workflows.pop(job.seq)
            handle._resolve_failure(
                temporalio.exceptions.failure_to_error(
                    job.cancelled.failure, self._payload_converter
                )
            )
        else:
            raise RuntimeError("Child workflow start did not have status")

    def _apply_signal_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.SignalWorkflow
    ) -> None:
        # If there is no definition or dynamic, we buffer and ignore
        if job.signal_name not in self._signals and None not in self._signals:
            self._buffered_signals.setdefault(job.signal_name, []).append(job)
            return

        # Just find the arg types for now. The interceptor will be responsible
        # for checking whether the signal definition actually exists.
        signal_defn = self._defn.signals.get(job.signal_name)
        arg_types: Optional[List[Type]] = None
        if signal_defn:
            arg_types, _ = self._type_lookup.get_type_hints(signal_defn.fn)
        args = self._convert_payloads(job.input, arg_types)

        # Schedule the handler
        self.create_task(
            self._inbound.handle_signal(
                HandleSignalInput(name=job.signal_name, args=args)
            )
        )

    def _apply_start_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.StartWorkflow
    ) -> None:
        # Async call to run on the scheduler thread
        async def run_workflow(input: ExecuteWorkflowInput) -> None:
            command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
            # We intentionally don't catch all errors, instead we bubble those
            # out as task failures
            try:
                success = await self._inbound.execute_workflow(input)
                result_payloads = self._payload_converter.to_payloads([success])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command.complete_workflow_execution.result.CopyFrom(
                    temporalio.bridge.worker.to_bridge_payload(result_payloads[0])
                )
                self._pending_commands.append(command)
            except temporalio.exceptions.FailureError as err:
                logger.debug(
                    f"Workflow raised failure with run ID {self._info.run_id}",
                    exc_info=True,
                )
                command.fail_workflow_execution.failure.SetInParent()
                try:
                    temporalio.exceptions.apply_exception_to_failure(
                        err,
                        self._payload_converter,
                        command.fail_workflow_execution.failure,
                    )
                    self._pending_commands.append(command)
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting workflow exception"
                    ) from inner_err
            except asyncio.CancelledError as err:
                command.fail_workflow_execution.failure.SetInParent()
                temporalio.exceptions.apply_exception_to_failure(
                    temporalio.exceptions.CancelledError(str(err)),
                    self._payload_converter,
                    command.fail_workflow_execution.failure,
                )
                self._pending_commands.append(command)

        # Schedule it
        arg_types, _ = self._type_lookup.get_type_hints(self._defn.run_fn)
        input = ExecuteWorkflowInput(
            type=self._defn.cls,
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            run_fn=cast(Callable[..., Awaitable[Any]], self._defn.run_fn),
            args=self._convert_payloads(job.arguments, arg_types),
        )
        self._primary_task = self.create_task(run_workflow(input))

    #### _Runtime direct workflow call overrides ####
    # These are in alphabetical order and all start with "workflow_".

    async def workflow_continue_as_new(
        self,
        *args: Any,
        workflow: Union[None, Callable, str],
        task_queue: Optional[str],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[Mapping[str, Any]],
    ) -> NoReturn:
        # Use definition if callable
        name: Optional[str] = None
        arg_types: Optional[List[Type]] = None
        if isinstance(workflow, str):
            name = workflow
        elif callable(workflow):
            defn = temporalio.workflow._Definition.must_from_run_fn(workflow)
            name = defn.name
            arg_types, _ = self._type_lookup.get_type_hints(defn.run_fn)
        elif workflow is not None:
            raise TypeError("Workflow must be None, a string, or callable")

        await self._outbound.continue_as_new(
            ContinueAsNewInput(
                workflow=name,
                args=args,
                task_queue=task_queue,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                memo=memo,
                search_attributes=search_attributes,
                arg_types=arg_types,
            )
        )
        raise RuntimeError("Unreachable")

    def workflow_info(self) -> temporalio.workflow.Info:
        return self._outbound.info()

    def workflow_now(self) -> datetime:
        return datetime.utcfromtimestamp(asyncio.get_running_loop().time())

    def workflow_start_activity(
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
            arg_types, ret_type = self._type_lookup.get_type_hints(activity)
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

    async def workflow_start_child_workflow(
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
            arg_types, ret_type = self._type_lookup.get_type_hints(defn.run_fn)
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

    def workflow_start_local_activity(
        self,
        activity: Any,
        *args: Any,
        activity_id: Optional[str],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        local_retry_threshold: Optional[timedelta],
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
            arg_types, ret_type = self._type_lookup.get_type_hints(activity)
        else:
            raise TypeError("Activity must be a string or callable")

        return self._outbound.start_local_activity(
            StartLocalActivityInput(
                activity=name,
                args=args,
                activity_id=activity_id,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                retry_policy=retry_policy,
                local_retry_threshold=local_retry_threshold,
                cancellation_type=cancellation_type,
                arg_types=arg_types,
                ret_type=ret_type,
            )
        )

    async def workflow_wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        fut = self.create_future()
        self._conditions.append((fn, fut))
        await asyncio.wait_for(fut, timeout)

    #### Calls from outbound impl ####
    # These are in alphabetical order and all start with "_outbound_".

    async def _outbound_continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        # Just add the command, force stop the loop, and wait on a
        # never-resolved future
        command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
        v = command.continue_as_new_workflow_execution
        if input.workflow:
            v.workflow_type = input.workflow
        if input.task_queue:
            v.task_queue = input.task_queue
        if input.args:
            v.arguments.extend(
                temporalio.bridge.worker.to_bridge_payloads(
                    self._payload_converter.to_payloads(input.args)
                )
            )
        if input.run_timeout:
            v.workflow_run_timeout.FromTimedelta(input.run_timeout)
        if input.task_timeout:
            v.workflow_task_timeout.FromTimedelta(input.task_timeout)
        # TODO(cretz): Headers
        # v.headers = input.he
        if input.memo:
            for k, val in input.memo.items():
                v.memo[k] = temporalio.bridge.worker.to_bridge_payload(
                    self._payload_converter.to_payloads([val])[0]
                )
        if input.search_attributes:
            for k, val in input.search_attributes.items():
                v.search_attributes[k] = temporalio.bridge.worker.to_bridge_payload(
                    # We have to use the default data converter for this
                    temporalio.converter.default().payload_converter.to_payloads([val])[
                        0
                    ]
                )
        self._pending_commands.append(command)
        self._force_stop_loop = True
        await self.create_future()
        raise RuntimeError("Unreachable")

    def _outbound_schedule_activity(
        self,
        input: Union[StartActivityInput, StartLocalActivityInput],
    ) -> _ActivityHandle:
        # Validate
        if not input.start_to_close_timeout and not input.schedule_to_close_timeout:
            raise ValueError(
                "Activity must have start_to_close_timeout or schedule_to_close_timeout"
            )

        seq = self._next_seq("activity")
        result_fut = self.create_future()

        # Function that runs in the handle
        async def run_activity() -> Any:
            nonlocal result_fut
            while True:
                try:
                    # We have to shield because we don't want the future itself
                    # to be cancelled
                    return await asyncio.shield(result_fut)
                except asyncio.CancelledError:
                    # Do nothing if the activity isn't known (may have completed
                    # already) or is already done
                    # TODO(cretz): What if the schedule command hasn't been sent yet?
                    handle = self._pending_activities.get(seq)
                    if handle and not handle.done():
                        self._pending_commands.append(handle._build_cancel_command())

        # Create the handle and set as pending
        handle = _ActivityHandle(self, seq, input, result_fut, run_activity())
        self._pending_activities[seq] = handle
        self._pending_commands.append(handle._build_schedule_command())
        return handle

    async def _outbound_start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> _ChildWorkflowHandle:
        seq = self._next_seq("child_workflow")
        start_fut = self.create_future()
        result_fut = self.create_future()

        # Function that runs in the handle
        async def run_child() -> Any:
            nonlocal start_fut, result_fut
            while True:
                try:
                    # We have to shield because we don't want the future itself
                    # to be cancelled
                    await asyncio.shield(start_fut)
                    return await asyncio.shield(result_fut)
                except asyncio.CancelledError:
                    # Do nothing if the child isn't known (may have completed
                    # already) or is already done
                    # TODO(cretz): What if the schedule command hasn't been sent yet?
                    if handle and not handle.done():
                        self._pending_commands.append(handle.build_cancel_command())

        # Create the handle and set as pending
        handle = _ChildWorkflowHandle(
            self, seq, input, start_fut, result_fut, run_child()
        )
        self._pending_child_workflows[seq] = handle
        self._pending_commands.append(handle._build_start_command())

        # Wait on start
        await start_fut
        return handle

    #### Miscellaneous helpers ####
    # These are in alphabetical order.

    def _check_condition(self, fn: Callable[[], bool], fut: asyncio.Future) -> bool:
        if fn():
            fut.set_result(True)
            return True
        return False

    def _convert_payloads(
        self,
        payloads: Sequence[temporalio.bridge.proto.common.Payload],
        types: Optional[List[Type]],
    ) -> List[Any]:
        if not payloads:
            return []
        # Only use type hints if they match count
        if types and len(types) != len(payloads):
            types = None
        try:
            return self._payload_converter.from_payloads(
                temporalio.bridge.worker.from_bridge_payloads(payloads),
                type_hints=types,
            )
        except Exception as err:
            raise RuntimeError("Failed decoding arguments") from err

    def _next_seq(self, type: str) -> int:
        seq = self._curr_seqs.get(type, 0) + 1
        self._curr_seqs[type] = seq
        return seq

    def _run_once(self) -> None:
        try:
            asyncio._set_running_loop(self)

            # We instantiate the workflow class _inside_ here because __init__
            # needs to run with this event loop set
            if not self._object:
                self._object = self._defn.cls()

            # Run while there is anything ready
            while self._ready and not self._force_stop_loop:

                # Run and remove all ready ones
                while self._ready and not self._force_stop_loop:
                    handle = self._ready.popleft()
                    handle._run()

                # Check conditions which may add to the ready list
                self._conditions[:] = [
                    t for t in self._conditions if not self._check_condition(t[0], t[1])
                ]
        finally:
            asyncio._set_running_loop(None)

    #### asyncio.AbstractEventLoop function impls ####
    # These are in the order defined in CPython's impl of the base class. Many
    # functions are intentionally not implemented/supported.

    def _timer_handle_cancelled(self, handle: asyncio.TimerHandle) -> None:
        if not isinstance(handle, _TimerHandle):
            raise TypeError("Expected Temporal timer handle")
        if not self._pending_timers.pop(handle._seq, None):
            return
        self._pending_commands.append(handle._build_cancel_command())

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

        # Create, schedule, and return
        seq = self._next_seq("timer")
        handle = _TimerHandle(seq, self._time + delay, callback, args, self, context)
        self._pending_commands.append(handle._build_start_command(delay))
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
        # Name not supported in older Python versions
        if sys.version_info < (3, 8):
            return asyncio.Task(coro, loop=self)
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


class _WorkflowInboundImpl(WorkflowInboundInterceptor):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
    ) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._instance = instance

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self._outbound = outbound

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        args = [self._instance._object] + list(input.args)
        return await input.run_fn(*args)

    async def handle_signal(self, input: HandleSignalInput) -> None:
        # Get the definition or fall through to dynamic
        signal_defn = self._instance._signals.get(
            input.name
        ) or self._instance._signals.get(None)
        # Presence of the signal handler was already checked, but they could
        # change the name so we re-check
        if not signal_defn:
            raise RuntimeError(
                f"signal handler for {input.name} expected but not found"
            )
        # Put self as arg first, then name only if dynamic
        args: Iterable[Any]
        if signal_defn.name:
            args = [self._instance._object] + list(input.args)
        else:
            args = [self._instance._object, input.name] + list(input.args)
        if inspect.iscoroutinefunction(signal_defn.fn):
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            signal_fn = cast(Callable[..., Awaitable[None]], signal_defn.fn)
            await signal_fn(*args)
        else:
            signal_fn = cast(Callable, signal_defn.fn)
            signal_fn(*args)

    async def handle_query(self, input: HandleQueryInput) -> Any:
        # Get the definition or fall through to dynamic
        query_defn = self._instance._queries.get(
            input.name
        ) or self._instance._queries.get(None)
        if not query_defn:
            raise RuntimeError(f"Workflow did not register a handler for {input.name}")
        # Put self as arg first, then name only if dynamic
        args: Iterable[Any]
        if query_defn.name:
            args = [self._instance._object] + list(input.args)
        else:
            args = [self._instance._object, input.name] + list(input.args)
        if inspect.iscoroutinefunction(query_defn.fn):
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            query_fn = cast(Callable[..., Awaitable[None]], query_defn.fn)
            return await query_fn(*args)
        else:
            query_fn = cast(Callable, query_defn.fn)
            return query_fn(*args)


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, instance: _WorkflowInstanceImpl) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._instance = instance

    async def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        await self._instance._outbound_continue_as_new(input)

    def info(self) -> temporalio.workflow.Info:
        return self._instance._info

    def start_activity(
        self, input: StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        return self._instance._outbound_schedule_activity(input)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        return await self._instance._outbound_start_child_workflow(input)

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        return self._instance._outbound_schedule_activity(input)


class _TimerHandle(asyncio.TimerHandle):
    def __init__(
        self,
        seq: int,
        when: float,
        callback: Callable[..., Any],
        args: Sequence[Any],
        loop: asyncio.AbstractEventLoop,
        context: Optional[contextvars.Context],
    ) -> None:
        super().__init__(when, callback, args, loop, context)
        self._seq = seq

    def _build_start_command(
        self, delay: float
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
        command.start_timer.seq = self._seq
        command.start_timer.start_to_fire_timeout.FromNanoseconds(int(delay * 1e9))
        return command

    def _build_cancel_command(
        self,
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        return temporalio.bridge.proto.workflow_commands.WorkflowCommand(
            cancel_timer=temporalio.bridge.proto.workflow_commands.CancelTimer(
                seq=self._seq
            ),
        )


class _ActivityHandle(temporalio.workflow.ActivityHandle[Any]):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        seq: int,
        input: Union[StartActivityInput, StartLocalActivityInput],
        result_fut: asyncio.Future,
        fn: Awaitable[Any],
    ) -> None:
        # TODO(cretz): Customize name in 3.9+?
        super().__init__(fn)
        self._instance = instance
        self._seq = seq
        self._input = input
        self._result_fut = result_fut

    def _resolve_success(self, result: Any) -> None:
        if not self._result_fut.done():
            self._result_fut.set_result(result)

    def _resolve_failure(self, err: Exception) -> None:
        if not self._result_fut.done():
            self._result_fut.set_exception(err)

    def _build_schedule_command(
        self,
        local_backoff: Optional[
            temporalio.bridge.proto.activity_result.DoBackoff
        ] = None,
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
        # TODO(cretz): Why can't MyPy infer this?
        v: Union[
            temporalio.bridge.proto.workflow_commands.ScheduleActivity,
            temporalio.bridge.proto.workflow_commands.ScheduleLocalActivity,
        ] = (
            command.schedule_local_activity
            if isinstance(self._input, StartLocalActivityInput)
            else command.schedule_activity
        )
        v.seq = self._seq
        v.activity_id = self._input.activity_id or str(self._seq)
        v.activity_type = self._input.activity
        # TODO(cretz): Headers
        # v.headers = input.he
        if self._input.args:
            v.arguments.extend(
                temporalio.bridge.worker.to_bridge_payloads(
                    self._instance._payload_converter.to_payloads(self._input.args)
                )
            )
        if self._input.schedule_to_close_timeout:
            v.schedule_to_close_timeout.FromTimedelta(
                self._input.schedule_to_close_timeout
            )
        if self._input.schedule_to_start_timeout:
            v.schedule_to_start_timeout.FromTimedelta(
                self._input.schedule_to_start_timeout
            )
        if self._input.start_to_close_timeout:
            v.start_to_close_timeout.FromTimedelta(self._input.start_to_close_timeout)
        if self._input.retry_policy:
            temporalio.bridge.worker.retry_policy_to_proto(
                self._input.retry_policy, v.retry_policy
            )
        v.cancellation_type = cast(
            "temporalio.bridge.proto.workflow_commands.ActivityCancellationType.ValueType",
            int(self._input.cancellation_type),
        )

        # Things specific to local or remote
        if isinstance(self._input, StartActivityInput):
            command.schedule_activity.task_queue = (
                self._input.task_queue or self._instance._info.task_queue
            )
            if self._input.heartbeat_timeout:
                command.schedule_activity.heartbeat_timeout.FromTimedelta(
                    self._input.heartbeat_timeout
                )
        if isinstance(self._input, StartLocalActivityInput):
            if self._input.local_retry_threshold:
                command.schedule_local_activity.local_retry_threshold.FromTimedelta(
                    self._input.local_retry_threshold
                )
            if local_backoff:
                command.schedule_local_activity.attempt = local_backoff.attempt
                command.schedule_local_activity.original_schedule_time.CopyFrom(
                    local_backoff.original_schedule_time
                )
            # TODO(cretz): Remove when https://github.com/temporalio/sdk-core/issues/316 fixed
            command.schedule_local_activity.retry_policy.SetInParent()
        return command

    def _build_cancel_command(
        self,
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        if isinstance(self._input, StartActivityInput):
            return temporalio.bridge.proto.workflow_commands.WorkflowCommand(
                request_cancel_activity=temporalio.bridge.proto.workflow_commands.RequestCancelActivity(
                    seq=self._seq
                ),
            )
        return temporalio.bridge.proto.workflow_commands.WorkflowCommand(
            request_cancel_local_activity=temporalio.bridge.proto.workflow_commands.RequestCancelLocalActivity(
                seq=self._seq
            ),
        )


class _ChildWorkflowHandle(temporalio.workflow.ChildWorkflowHandle[Any, Any]):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        seq: int,
        input: StartChildWorkflowInput,
        start_fut: asyncio.Future[None],
        result_fut: asyncio.Future[Any],
        fn: Awaitable[Any],
    ) -> None:
        # TODO(cretz): Customize name in 3.9+?
        super().__init__(fn)
        self._instance = instance
        self._seq = seq
        self._input = input
        self._start_fut = start_fut
        self._result_fut = result_fut
        self._original_run_id = "<unknown>"

    @property
    def id(self) -> str:
        return self._input.id

    @property
    def original_run_id(self) -> Optional[str]:
        return self._original_run_id

    async def signal(
        self,
        signal: Union[str, Callable],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Iterable[Any] = [],
    ) -> None:
        raise NotImplementedError

    def _resolve_start_success(self, run_id: str) -> None:
        self._original_run_id = run_id
        if not self._start_fut.done():
            self._start_fut.set_result(None)

    def _resolve_success(self, result: Any) -> None:
        if not self._result_fut.done():
            self._result_fut.set_result(result)

    def _resolve_failure(self, err: Exception) -> None:
        if not self._start_fut.done():
            self._start_fut.set_exception(err)
        if not self._result_fut.done():
            self._result_fut.set_exception(err)

    def _build_start_command(
        self,
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        command = temporalio.bridge.proto.workflow_commands.WorkflowCommand()
        v = command.start_child_workflow_execution
        v.seq = self._seq
        v.namespace = self._input.namespace or self._instance._info.namespace
        v.workflow_id = self._input.id
        v.workflow_type = self._input.workflow
        v.task_queue = self._input.task_queue or self._instance._info.task_queue
        if self._input.args:
            v.input.extend(
                temporalio.bridge.worker.to_bridge_payloads(
                    self._instance._payload_converter.to_payloads(self._input.args)
                )
            )
        if self._input.execution_timeout:
            v.workflow_execution_timeout.FromTimedelta(self._input.execution_timeout)
        if self._input.run_timeout:
            v.workflow_run_timeout.FromTimedelta(self._input.run_timeout)
        if self._input.task_timeout:
            v.workflow_task_timeout.FromTimedelta(self._input.task_timeout)
        v.parent_close_policy = cast(
            "temporalio.bridge.proto.child_workflow.ParentClosePolicy.ValueType",
            int(self._input.parent_close_policy),
        )
        v.workflow_id_reuse_policy = cast(
            "temporalio.bridge.proto.common.WorkflowIdReusePolicy.ValueType",
            int(self._input.id_reuse_policy),
        )
        if self._input.retry_policy:
            temporalio.bridge.worker.retry_policy_to_proto(
                self._input.retry_policy, v.retry_policy
            )
        v.cron_schedule = self._input.cron_schedule
        # TODO(cretz): Headers
        # v.headers = input.he
        if self._input.memo:
            for k, val in self._input.memo.items():
                v.memo[k] = temporalio.bridge.worker.to_bridge_payload(
                    self._instance._payload_converter.to_payloads([val])[0]
                )
        if self._input.search_attributes:
            for k, val in self._input.search_attributes.items():
                v.search_attributes[k] = temporalio.bridge.worker.to_bridge_payload(
                    # We have to use the default data converter for this
                    (
                        temporalio.converter.default().payload_converter.to_payloads(
                            [val]
                        )
                    )[0]
                )
        v.cancellation_type = cast(
            "temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.ValueType",
            int(self._input.cancellation_type),
        )
        return command

    def build_cancel_command(
        self,
    ) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        if self._start_fut.done():
            return temporalio.bridge.proto.workflow_commands.WorkflowCommand(
                request_cancel_external_workflow_execution=temporalio.bridge.proto.workflow_commands.RequestCancelExternalWorkflowExecution(
                    seq=self._seq,
                    child_workflow_id=self._input.id,
                ),
            )
        return temporalio.bridge.proto.workflow_commands.WorkflowCommand(
            cancel_unstarted_child_workflow_execution=temporalio.bridge.proto.workflow_commands.CancelUnstartedChildWorkflowExecution(
                child_workflow_seq=self._seq,
            ),
        )
