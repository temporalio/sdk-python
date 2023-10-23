"""Workflow worker runner and instance."""

from __future__ import annotations

import asyncio
import collections
import contextvars
import inspect
import logging
import random
import sys
import traceback
import warnings
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Generator,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

import google.protobuf.empty_pb2
from typing_extensions import TypeAlias, TypedDict

import temporalio.activity
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.proto.workflow_completion
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow

from ._interceptor import (
    ContinueAsNewInput,
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    HandleUpdateInput,
    SignalChildWorkflowInput,
    SignalExternalWorkflowInput,
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
    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        """Prepare a workflow for future execution.

        This is run once for each workflow definition when a worker starts. This
        allows the runner to do anything necessarily to prepare for this
        definition to be used multiple times in create_instance.

        Args:
            defn: The workflow definition.
        """
        raise NotImplementedError

    @abstractmethod
    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        """Create a workflow instance that can handle activations.

        Args:
            det: Details that can be used to create the instance.

        Returns:
            Workflow instance that can handle activations.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class WorkflowInstanceDetails:
    """Immutable details for creating a workflow instance."""

    payload_converter_class: Type[temporalio.converter.PayloadConverter]
    failure_converter_class: Type[temporalio.converter.FailureConverter]
    interceptor_classes: Sequence[Type[WorkflowInboundInterceptor]]
    defn: temporalio.workflow._Definition
    info: temporalio.workflow.Info
    randomness_seed: int
    extern_functions: Mapping[str, Callable]
    disable_eager_activity_execution: bool


class WorkflowInstance(ABC):
    """Instance of a workflow that can handle activations."""

    @abstractmethod
    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        """Handle an activation and return completion.

        This should never raise an exception, but instead catch all exceptions
        and set as completion failure.

        Args:
            act: Protobuf activation.

        Returns:
            Completion object with successful commands set or failure info set.
        """
        raise NotImplementedError


class UnsandboxedWorkflowRunner(WorkflowRunner):
    """Workflow runner that does not do any sandboxing."""

    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        """Implements :py:meth:`WorkflowRunner.prepare_workflow` as a no-op."""
        pass

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        """Implements :py:meth:`WorkflowRunner.create_instance`."""
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
        self._failure_converter = det.failure_converter_class()
        self._defn = det.defn
        self._info = det.info
        self._extern_functions = det.extern_functions
        self._disable_eager_activity_execution = det.disable_eager_activity_execution
        self._primary_task: Optional[asyncio.Task[None]] = None
        self._time_ns = 0
        self._cancel_requested = False
        self._current_history_length = 0
        self._current_history_size = 0
        self._continue_as_new_suggested = False
        # Lazily loaded
        self._memo: Optional[Mapping[str, Any]] = None
        # Handles which are ready to run on the next event loop iteration
        self._ready: Deque[asyncio.Handle] = collections.deque()
        self._conditions: List[Tuple[Callable[[], bool], asyncio.Future]] = []
        # Keyed by seq
        self._pending_timers: Dict[int, _TimerHandle] = {}
        self._pending_activities: Dict[int, _ActivityHandle] = {}
        self._pending_child_workflows: Dict[int, _ChildWorkflowHandle] = {}
        self._pending_external_signals: Dict[int, asyncio.Future] = {}
        self._pending_external_cancels: Dict[int, asyncio.Future] = {}
        # Keyed by type
        self._curr_seqs: Dict[str, int] = {}
        # TODO(cretz): Any concerns about not sharing this? Maybe the types I
        # need to lookup should be done at definition time?
        self._exception_handler: Optional[_ExceptionHandler] = None
        # The actual instance, instantiated on first _run_once
        self._object: Any = None
        self._is_replaying: bool = False
        self._random = random.Random(det.randomness_seed)
        self._read_only = False

        # Patches we have been notified of and memoized patch responses
        self._patches_notified: Set[str] = set()
        self._patches_memoized: Dict[str, bool] = {}

        # Tasks stored by asyncio are weak references and therefore can get GC'd
        # which can cause warnings like "Task was destroyed but it is pending!".
        # So we store the tasks ourselves.
        # See https://bugs.python.org/issue21163 and others.
        self._tasks: Set[asyncio.Task] = set()

        # We maintain signals, queries, and updates on this class since handlers can be
        # added during workflow execution
        self._signals = dict(self._defn.signals)
        self._queries = dict(self._defn.queries)
        self._updates = dict(self._defn.updates)

        # Add stack trace handler
        # TODO(cretz): Is it ok that this can be forcefully overridden by the
        # workflow author? They could technically override in interceptor
        # anyways. Putting here ensures it ends up in the query list on
        # query-not-found error.
        self._queries["__stack_trace"] = temporalio.workflow._QueryDefinition(
            name="__stack_trace",
            fn=self._stack_trace,
            is_method=False,
            arg_types=[],
            ret_type=str,
        )

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

        # After GC, Python raises GeneratorExit calls from all awaiting tasks.
        # Then in a finally of such an await, another exception can swallow
        # these causing even more issues. We will set ourselves as deleted so we
        # can check in some places to swallow these errors on tear down.
        self._deleting = False

        # We only create the metric meter lazily
        self._metric_meter: Optional[_ReplaySafeMetricMeter] = None

    def __del__(self) -> None:
        # We have confirmed there are no super() versions of __del__
        self._deleting = True

    #### Activation functions ####
    # These are in alphabetical order and besides "activate", all other calls
    # are "_apply_" + the job field name.

    def activate(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion:
        # Reset current completion, time, and whether replaying
        self._current_completion = (
            temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion()
        )
        self._current_completion.successful.SetInParent()
        self._current_activation_error: Optional[Exception] = None
        self._current_history_length = act.history_length
        self._current_history_size = act.history_size_bytes
        self._continue_as_new_suggested = act.continue_as_new_suggested
        self._time_ns = act.timestamp.ToNanoseconds()
        self._is_replaying = act.is_replaying

        activation_err: Optional[Exception] = None
        try:
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
            for index, job_set in enumerate(job_sets):
                if not job_set:
                    continue
                for job in job_set:
                    # Let errors bubble out of these to the caller to fail the task
                    self._apply(job)

                # Run one iteration of the loop. We do not allow conditions to
                # be checked in patch jobs (first index) or query jobs (last
                # index).
                self._run_once(check_conditions=index == 1 or index == 2)
        except temporalio.exceptions.FailureError as err:
            # We want failure errors during activation, like those that can
            # happen during payload conversion, to fail the workflow not the
            # task
            try:
                self._set_workflow_failure(err)
            except Exception as inner_err:
                activation_err = inner_err
        except Exception as err:
            activation_err = err
        if activation_err:
            logger.warning(
                f"Failed activation on workflow {self._info.workflow_type} with ID {self._info.workflow_id} and run ID {self._info.run_id}",
                exc_info=activation_err,
            )
            # Set completion failure
            self._current_completion.failed.failure.SetInParent()
            try:
                self._failure_converter.to_failure(
                    activation_err,
                    self._payload_converter,
                    self._current_completion.failed.failure,
                )
            except Exception as inner_err:
                logger.exception(
                    f"Failed converting activation exception on workflow with run ID {act.run_id}"
                )
                self._current_completion.failed.failure.message = (
                    f"Failed converting activation exception: {inner_err}"
                )

        # If there are successful commands, we must remove all
        # non-query-responses after terminal workflow commands. We must do this
        # in place to avoid the copy-on-write that occurs when you reassign.
        seen_completion = False
        i = 0
        while i < len(self._current_completion.successful.commands):
            command = self._current_completion.successful.commands[i]
            if not seen_completion:
                seen_completion = (
                    command.HasField("complete_workflow_execution")
                    or command.HasField("continue_as_new_workflow_execution")
                    or command.HasField("fail_workflow_execution")
                )
            elif not command.HasField("respond_to_query"):
                del self._current_completion.successful.commands[i]
                continue
            i += 1

        return self._current_completion

    def _apply(
        self, job: temporalio.bridge.proto.workflow_activation.WorkflowActivationJob
    ) -> None:
        if job.HasField("cancel_workflow"):
            self._apply_cancel_workflow(job.cancel_workflow)
        elif job.HasField("do_update"):
            self._apply_do_update(job.do_update)
        elif job.HasField("fire_timer"):
            self._apply_fire_timer(job.fire_timer)
        elif job.HasField("query_workflow"):
            self._apply_query_workflow(job.query_workflow)
        elif job.HasField("notify_has_patch"):
            self._apply_notify_has_patch(job.notify_has_patch)
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
            self._apply_resolve_request_cancel_external_workflow(
                job.resolve_request_cancel_external_workflow
            )
        elif job.HasField("resolve_signal_external_workflow"):
            self._apply_resolve_signal_external_workflow(
                job.resolve_signal_external_workflow
            )
        elif job.HasField("signal_workflow"):
            self._apply_signal_workflow(job.signal_workflow)
        elif job.HasField("start_workflow"):
            self._apply_start_workflow(job.start_workflow)
        elif job.HasField("update_random_seed"):
            self._apply_update_random_seed(job.update_random_seed)
        else:
            raise RuntimeError(f"Unrecognized job: {job.WhichOneof('variant')}")

    def _apply_cancel_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.CancelWorkflow
    ) -> None:
        self._cancel_requested = True
        # TODO(cretz): Details or cancel message or whatever?
        if self._primary_task:
            # The primary task may not have started yet and we want to give the
            # workflow the ability to receive the cancellation, so we must defer
            # this cancellation to the next iteration of the event loop.
            self.call_soon(self._primary_task.cancel)

    def _apply_do_update(
        self, job: temporalio.bridge.proto.workflow_activation.DoUpdate
    ):
        # Run the validator & handler in a task. Everything, including looking up the update definition, needs to be
        # inside the task, since the update may not be defined until after we have started the workflow - for example
        # if an update is in the first WFT & is also registered dynamically at the top of workflow code.
        async def run_update() -> None:
            command = self._add_command()
            command.update_response.protocol_instance_id = job.protocol_instance_id
            try:
                defn = self._updates.get(job.name) or self._updates.get(None)
                if not defn:
                    known_updates = sorted([k for k in self._updates.keys() if k])
                    raise RuntimeError(
                        f"Update handler for '{job.name}' expected but not found, and there is no dynamic handler. "
                        f"known updates: [{' '.join(known_updates)}]"
                    )
                args = self._process_handler_args(
                    job.name,
                    job.input,
                    defn.name,
                    defn.arg_types,
                    defn.dynamic_vararg,
                )
                handler_input = HandleUpdateInput(
                    id=job.id,
                    update=job.name,
                    args=args,
                    headers=job.headers,
                )

                # Always run the validator interceptor, which will only actually run a validator if one is defined.
                with self._as_read_only():
                    self._inbound.handle_update_validator(handler_input)

                # Accept the update
                command.update_response.accepted.SetInParent()
                command = None  # type: ignore

                # Run the handler
                success = await self._inbound.handle_update_handler(handler_input)
                result_payloads = self._payload_converter.to_payloads([success])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command = self._add_command()
                command.update_response.protocol_instance_id = job.protocol_instance_id
                command.update_response.completed.CopyFrom(result_payloads[0])
            except (Exception, asyncio.CancelledError) as err:
                logger.debug(
                    f"Update raised failure with run ID {self._info.run_id}",
                    exc_info=True,
                )
                # All asyncio cancelled errors become Temporal cancelled errors
                if isinstance(err, asyncio.CancelledError):
                    err = temporalio.exceptions.CancelledError(
                        f"Cancellation raised within update {err}"
                    )
                # Read-only issues during validation should fail the task
                if isinstance(err, temporalio.workflow.ReadOnlyContextError):
                    self._current_activation_error = err
                    return
                # All other errors fail the update
                if command is None:
                    command = self._add_command()
                    command.update_response.protocol_instance_id = (
                        job.protocol_instance_id
                    )
                self._failure_converter.to_failure(
                    err,
                    self._payload_converter,
                    command.update_response.rejected.cause,
                )
            except BaseException as err:
                # During tear down, generator exit and no-runtime exceptions can appear
                if not self._deleting:
                    raise
                if not isinstance(
                    err,
                    (
                        GeneratorExit,
                        temporalio.workflow._NotInWorkflowEventLoopError,
                    ),
                ):
                    logger.debug(
                        "Ignoring exception while deleting workflow", exc_info=True
                    )

        self.create_task(
            run_update(),
            name=f"update: {job.name}",
        )

    def _apply_fire_timer(
        self, job: temporalio.bridge.proto.workflow_activation.FireTimer
    ) -> None:
        # We ignore an absent handler because it may have been cancelled and
        # removed earlier this activation by a signal
        handle = self._pending_timers.pop(job.seq, None)
        if handle:
            self._ready.append(handle)

    def _apply_query_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.QueryWorkflow
    ) -> None:
        # Wrap entire bunch of work in a task
        async def run_query() -> None:
            command = self._add_command()
            command.respond_to_query.query_id = job.query_id
            try:
                with self._as_read_only():
                    # Named query or dynamic
                    defn = self._queries.get(job.query_type) or self._queries.get(None)
                    if not defn:
                        known_queries = sorted([k for k in self._queries.keys() if k])
                        raise RuntimeError(
                            f"Query handler for '{job.query_type}' expected but not found, "
                            f"known queries: [{' '.join(known_queries)}]"
                        )

                    # Create input
                    args = self._process_handler_args(
                        job.query_type,
                        job.arguments,
                        defn.name,
                        defn.arg_types,
                        defn.dynamic_vararg,
                    )
                    input = HandleQueryInput(
                        id=job.query_id,
                        query=job.query_type,
                        args=args,
                        headers=job.headers,
                    )
                    success = await self._inbound.handle_query(input)
                    result_payloads = self._payload_converter.to_payloads([success])
                    if len(result_payloads) != 1:
                        raise ValueError(
                            f"Expected 1 result payload, got {len(result_payloads)}"
                        )
                    command.respond_to_query.succeeded.response.CopyFrom(
                        result_payloads[0]
                    )
            except Exception as err:
                try:
                    self._failure_converter.to_failure(
                        err,
                        self._payload_converter,
                        command.respond_to_query.failed,
                    )
                except Exception as inner_err:
                    raise ValueError(
                        "Failed converting application error"
                    ) from inner_err

        # Schedule it
        self.create_task(run_query(), name=f"query: {job.query_type}")

    def _apply_notify_has_patch(
        self, job: temporalio.bridge.proto.workflow_activation.NotifyHasPatch
    ) -> None:
        self._patches_notified.add(job.patch_id)

    def _apply_resolve_activity(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveActivity
    ) -> None:
        handle = self._pending_activities.pop(job.seq, None)
        if not handle:
            raise RuntimeError(f"Failed finding activity handle for sequence {job.seq}")
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
                self._failure_converter.from_failure(
                    job.result.failed.failure, self._payload_converter
                )
            )
        elif job.result.HasField("cancelled"):
            handle._resolve_failure(
                self._failure_converter.from_failure(
                    job.result.cancelled.failure, self._payload_converter
                )
            )
        elif job.result.HasField("backoff"):
            handle._resolve_backoff(job.result.backoff)
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
                self._failure_converter.from_failure(
                    job.result.failed.failure, self._payload_converter
                )
            )
        elif job.result.HasField("cancelled"):
            handle._resolve_failure(
                self._failure_converter.from_failure(
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
            # We intentionally do not pop here because this same handle is used
            # for waiting on the entire workflow to complete
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
                    RuntimeError(f"Unknown child start fail cause: {job.failed.cause}")
                )
        elif job.HasField("cancelled"):
            self._pending_child_workflows.pop(job.seq)
            handle._resolve_failure(
                self._failure_converter.from_failure(
                    job.cancelled.failure, self._payload_converter
                )
            )
        else:
            raise RuntimeError("Child workflow start did not have a known status")

    def _apply_resolve_request_cancel_external_workflow(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveRequestCancelExternalWorkflow,
    ) -> None:
        fut = self._pending_external_cancels.pop(job.seq, None)
        if not fut:
            raise RuntimeError(
                f"Failed finding pending external cancel for sequence {job.seq}"
            )
        # We intentionally let this error if future is already done
        if job.HasField("failure"):
            fut.set_exception(
                self._failure_converter.from_failure(
                    job.failure, self._payload_converter
                )
            )
        else:
            fut.set_result(None)

    def _apply_resolve_signal_external_workflow(
        self,
        job: temporalio.bridge.proto.workflow_activation.ResolveSignalExternalWorkflow,
    ) -> None:
        fut = self._pending_external_signals.pop(job.seq, None)
        if not fut:
            raise RuntimeError(
                f"Failed finding pending external signal for sequence {job.seq}"
            )
        # We intentionally let this error if future is already done
        if job.HasField("failure"):
            fut.set_exception(
                self._failure_converter.from_failure(
                    job.failure, self._payload_converter
                )
            )
        else:
            fut.set_result(None)

    def _apply_signal_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.SignalWorkflow
    ) -> None:
        # Apply to named or to dynamic or buffer
        signal_defn = self._signals.get(job.signal_name) or self._signals.get(None)
        if not signal_defn:
            self._buffered_signals.setdefault(job.signal_name, []).append(job)
            return
        self._process_signal_job(signal_defn, job)

    def _apply_start_workflow(
        self, job: temporalio.bridge.proto.workflow_activation.StartWorkflow
    ) -> None:
        # Async call to run on the scheduler thread. This will be wrapped in
        # another function which applies exception handling.
        async def run_workflow(input: ExecuteWorkflowInput) -> None:
            try:
                result = await self._inbound.execute_workflow(input)
                result_payloads = self._payload_converter.to_payloads([result])
                if len(result_payloads) != 1:
                    raise ValueError(
                        f"Expected 1 result payload, got {len(result_payloads)}"
                    )
                command = self._add_command()
                command.complete_workflow_execution.result.CopyFrom(result_payloads[0])
            except BaseException as err:
                # During tear down, generator exit and event loop exceptions can occur
                if not self._deleting:
                    raise
                if not isinstance(
                    err,
                    (GeneratorExit, temporalio.workflow._NotInWorkflowEventLoopError),
                ):
                    logger.debug(
                        "Ignoring exception while deleting workflow", exc_info=True
                    )

        # Set arg types, using raw values for dynamic
        arg_types = self._defn.arg_types
        if not self._defn.name:
            # Dynamic is just the raw value for each input value
            arg_types = [temporalio.common.RawValue] * len(job.arguments)
        args = self._convert_payloads(job.arguments, arg_types)
        # Put args in a list if dynamic
        if not self._defn.name:
            args = [args]

        # Schedule it
        input = ExecuteWorkflowInput(
            type=self._defn.cls,
            # TODO(cretz): Remove cast when https://github.com/python/mypy/issues/5485 fixed
            run_fn=cast(Callable[..., Awaitable[Any]], self._defn.run_fn),
            args=args,
            headers=job.headers,
        )
        self._primary_task = self.create_task(
            self._run_top_level_workflow_function(run_workflow(input)),
            name=f"run",
        )

    def _apply_update_random_seed(
        self, job: temporalio.bridge.proto.workflow_activation.UpdateRandomSeed
    ) -> None:
        self._random.seed(job.randomness_seed)

    #### _Runtime direct workflow call overrides ####
    # These are in alphabetical order and all start with "workflow_".

    def workflow_continue_as_new(
        self,
        *args: Any,
        workflow: Union[None, Callable, str],
        task_queue: Optional[str],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[temporalio.common.SearchAttributes],
        versioning_intent: Optional[temporalio.workflow.VersioningIntent],
    ) -> NoReturn:
        self._assert_not_read_only("continue as new")
        # Use definition if callable
        name: Optional[str] = None
        arg_types: Optional[List[Type]] = None
        if isinstance(workflow, str):
            name = workflow
        elif callable(workflow):
            defn = temporalio.workflow._Definition.must_from_run_fn(workflow)
            name = defn.name
            arg_types = defn.arg_types
        elif workflow is not None:
            raise TypeError("Workflow must be None, a string, or callable")

        self._outbound.continue_as_new(
            ContinueAsNewInput(
                workflow=name,
                args=args,
                task_queue=task_queue,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                retry_policy=retry_policy,
                memo=memo,
                search_attributes=search_attributes,
                headers={},
                arg_types=arg_types,
                versioning_intent=versioning_intent,
            )
        )
        # TODO(cretz): Why can't MyPy infer the above never returns?
        raise RuntimeError("Unreachable")

    def workflow_extern_functions(self) -> Mapping[str, Callable]:
        return self._extern_functions

    def workflow_get_current_history_length(self) -> int:
        return self._current_history_length

    def workflow_get_current_history_size(self) -> int:
        return self._current_history_size

    def workflow_get_external_workflow_handle(
        self, id: str, *, run_id: Optional[str]
    ) -> temporalio.workflow.ExternalWorkflowHandle[Any]:
        return _ExternalWorkflowHandle(self, id, run_id)

    def workflow_get_query_handler(self, name: Optional[str]) -> Optional[Callable]:
        defn = self._queries.get(name)
        if not defn:
            return None
        # Bind if a method
        return defn.bind_fn(self._object) if defn.is_method else defn.fn

    def workflow_get_signal_handler(self, name: Optional[str]) -> Optional[Callable]:
        defn = self._signals.get(name)
        if not defn:
            return None
        # Bind if a method
        return defn.bind_fn(self._object) if defn.is_method else defn.fn

    def workflow_get_update_handler(self, name: Optional[str]) -> Optional[Callable]:
        defn = self._updates.get(name)
        if not defn:
            return None
        # Bind if a method
        return defn.bind_fn(self._object) if defn.is_method else defn.fn

    def workflow_get_update_validator(self, name: Optional[str]) -> Optional[Callable]:
        defn = self._updates.get(name) or self._updates.get(None)
        if not defn or not defn.validator:
            return None
        # Bind if a method
        return defn.bind_validator(self._object) if defn.is_method else defn.validator

    def workflow_info(self) -> temporalio.workflow.Info:
        return self._outbound.info()

    def workflow_is_continue_as_new_suggested(self) -> bool:
        return self._continue_as_new_suggested

    def workflow_is_replaying(self) -> bool:
        return self._is_replaying

    def workflow_memo(self) -> Mapping[str, Any]:
        if self._memo is None:
            self._memo = {
                k: self._payload_converter.from_payloads([v])[0]
                for k, v in self._info.raw_memo.items()
            }
        return self._memo

    def workflow_memo_value(
        self, key: str, default: Any, *, type_hint: Optional[Type]
    ) -> Any:
        payload = self._info.raw_memo.get(key)
        if not payload:
            if default is temporalio.common._arg_unset:
                raise KeyError(f"Memo does not have a value for key {key}")
            return default
        return self._payload_converter.from_payloads(
            [payload], [type_hint] if type_hint else None
        )[0]

    def workflow_metric_meter(self) -> temporalio.common.MetricMeter:
        # Create if not present, which means using an extern function
        if not self._metric_meter:
            metric_meter = cast(_WorkflowExternFunctions, self._extern_functions)[
                "__temporal_get_metric_meter"
            ]()
            metric_meter = metric_meter.with_additional_attributes(
                {
                    "namespace": self._info.namespace,
                    "task_queue": self._info.task_queue,
                    "workflow_type": self._info.workflow_type,
                }
            )
            self._metric_meter = _ReplaySafeMetricMeter(metric_meter)
        return self._metric_meter

    def workflow_patch(self, id: str, *, deprecated: bool) -> bool:
        self._assert_not_read_only("patch")
        # We use a previous memoized result of this if present. If this is being
        # deprecated, we can still use memoized result and skip the command.
        use_patch = self._patches_memoized.get(id)
        if use_patch is not None:
            return use_patch

        use_patch = not self._is_replaying or id in self._patches_notified
        self._patches_memoized[id] = use_patch
        if use_patch:
            command = self._add_command()
            command.set_patch_marker.patch_id = id
            command.set_patch_marker.deprecated = deprecated
        return use_patch

    def workflow_payload_converter(self) -> temporalio.converter.PayloadConverter:
        return self._payload_converter

    def workflow_random(self) -> random.Random:
        self._assert_not_read_only("random")
        return self._random

    def workflow_set_query_handler(
        self, name: Optional[str], handler: Optional[Callable]
    ) -> None:
        self._assert_not_read_only("set query handler")
        if handler:
            if inspect.iscoroutinefunction(handler):
                warnings.warn(
                    "Queries as async def functions are deprecated",
                    DeprecationWarning,
                    stacklevel=3,
                )
            defn = temporalio.workflow._QueryDefinition(
                name=name, fn=handler, is_method=False
            )
            self._queries[name] = defn
            if defn.dynamic_vararg:
                warnings.warn(
                    "Dynamic queries with vararg third param is deprecated, use Sequence[RawValue]",
                    DeprecationWarning,
                    stacklevel=3,
                )
        else:
            self._queries.pop(name, None)

    def workflow_set_signal_handler(
        self, name: Optional[str], handler: Optional[Callable]
    ) -> None:
        self._assert_not_read_only("set signal handler")
        if handler:
            defn = temporalio.workflow._SignalDefinition(
                name=name, fn=handler, is_method=False
            )
            self._signals[name] = defn
            if defn.dynamic_vararg:
                warnings.warn(
                    "Dynamic signals with vararg third param is deprecated, use Sequence[RawValue]",
                    DeprecationWarning,
                    stacklevel=3,
                )
            # We have to send buffered signals to the handler if they apply
            if name:
                for job in self._buffered_signals.pop(name, []):
                    self._process_signal_job(defn, job)
            else:
                for jobs in self._buffered_signals.values():
                    for job in jobs:
                        self._process_signal_job(defn, job)
                self._buffered_signals.clear()
        else:
            self._signals.pop(name, None)

    def workflow_set_update_handler(
        self,
        name: Optional[str],
        handler: Optional[Callable],
        validator: Optional[Callable],
    ) -> None:
        self._assert_not_read_only("set update handler")
        if handler:
            defn = temporalio.workflow._UpdateDefinition(
                name=name, fn=handler, is_method=False
            )
            if validator is not None:
                defn.set_validator(validator)
            self._updates[name] = defn
            if defn.dynamic_vararg:
                raise RuntimeError(
                    "Dynamic updates do not support a vararg third param, use Sequence[RawValue]",
                )
        else:
            self._updates.pop(name, None)

    def workflow_start_activity(
        self,
        activity: Any,
        *args: Any,
        task_queue: Optional[str],
        result_type: Optional[Type],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        heartbeat_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cancellation_type: temporalio.workflow.ActivityCancellationType,
        activity_id: Optional[str],
        versioning_intent: Optional[temporalio.workflow.VersioningIntent],
    ) -> temporalio.workflow.ActivityHandle[Any]:
        self._assert_not_read_only("start activity")
        # Get activity definition if it's callable
        name: str
        arg_types: Optional[List[Type]] = None
        ret_type = result_type
        if isinstance(activity, str):
            name = activity
        elif callable(activity):
            defn = temporalio.activity._Definition.must_from_callable(activity)
            if not defn.name:
                raise ValueError("Cannot invoke dynamic activity explicitly")
            name = defn.name
            arg_types = defn.arg_types
            ret_type = defn.ret_type
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
                headers={},
                disable_eager_execution=self._disable_eager_activity_execution,
                arg_types=arg_types,
                ret_type=ret_type,
                versioning_intent=versioning_intent,
            )
        )

    async def workflow_start_child_workflow(
        self,
        workflow: Any,
        *args: Any,
        id: str,
        task_queue: Optional[str],
        result_type: Optional[Type],
        cancellation_type: temporalio.workflow.ChildWorkflowCancellationType,
        parent_close_policy: temporalio.workflow.ParentClosePolicy,
        execution_timeout: Optional[timedelta],
        run_timeout: Optional[timedelta],
        task_timeout: Optional[timedelta],
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy,
        retry_policy: Optional[temporalio.common.RetryPolicy],
        cron_schedule: str,
        memo: Optional[Mapping[str, Any]],
        search_attributes: Optional[temporalio.common.SearchAttributes],
        versioning_intent: Optional[temporalio.workflow.VersioningIntent],
    ) -> temporalio.workflow.ChildWorkflowHandle[Any, Any]:
        # Use definition if callable
        name: str
        arg_types: Optional[List[Type]] = None
        ret_type = result_type
        if isinstance(workflow, str):
            name = workflow
        elif callable(workflow):
            defn = temporalio.workflow._Definition.must_from_run_fn(workflow)
            if not defn.name:
                raise TypeError("Cannot invoke dynamic workflow explicitly")
            name = defn.name
            arg_types = defn.arg_types
            ret_type = defn.ret_type
        else:
            raise TypeError("Workflow must be a string or callable")

        return await self._outbound.start_child_workflow(
            StartChildWorkflowInput(
                workflow=name,
                args=args,
                id=id,
                task_queue=task_queue,
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
                headers={},
                arg_types=arg_types,
                ret_type=ret_type,
                versioning_intent=versioning_intent,
            )
        )

    def workflow_start_local_activity(
        self,
        activity: Any,
        *args: Any,
        result_type: Optional[Type],
        schedule_to_close_timeout: Optional[timedelta],
        schedule_to_start_timeout: Optional[timedelta],
        start_to_close_timeout: Optional[timedelta],
        retry_policy: Optional[temporalio.common.RetryPolicy],
        local_retry_threshold: Optional[timedelta],
        cancellation_type: temporalio.workflow.ActivityCancellationType,
        activity_id: Optional[str],
    ) -> temporalio.workflow.ActivityHandle[Any]:
        # Get activity definition if it's callable
        name: str
        arg_types: Optional[List[Type]] = None
        ret_type = result_type
        if isinstance(activity, str):
            name = activity
        elif callable(activity):
            defn = temporalio.activity._Definition.must_from_callable(activity)
            if not defn.name:
                raise ValueError("Cannot invoke dynamic activity explicitly")
            name = defn.name
            arg_types = defn.arg_types
            ret_type = defn.ret_type
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
                headers={},
                arg_types=arg_types,
                ret_type=ret_type,
            )
        )

    def workflow_time_ns(self) -> int:
        return self._time_ns

    def workflow_upsert_search_attributes(
        self, attributes: temporalio.common.SearchAttributes
    ) -> None:
        v = self._add_command().upsert_workflow_search_attributes
        _encode_search_attributes(attributes, v.search_attributes)
        # Update the keys in the existing dictionary. We keep exact values sent
        # in instead of any kind of normalization. This means empty lists remain
        # as empty lists which matches what the server does. We know this is
        # mutable, so we can cast it as such.
        cast(MutableMapping, self._info.search_attributes).update(attributes)

    async def workflow_wait_condition(
        self, fn: Callable[[], bool], *, timeout: Optional[float] = None
    ) -> None:
        self._assert_not_read_only("wait condition")
        fut = self.create_future()
        self._conditions.append((fn, fut))
        await asyncio.wait_for(fut, timeout)

    #### Calls from outbound impl ####
    # These are in alphabetical order and all start with "_outbound_".

    def _outbound_continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        # Just throw
        raise _ContinueAsNewError(self, input)

    def _outbound_schedule_activity(
        self,
        input: Union[StartActivityInput, StartLocalActivityInput],
    ) -> _ActivityHandle:
        # Validate
        if not input.start_to_close_timeout and not input.schedule_to_close_timeout:
            raise ValueError(
                "Activity must have start_to_close_timeout or schedule_to_close_timeout"
            )

        handle: Optional[_ActivityHandle] = None

        # Function that runs in the handle
        async def run_activity() -> Any:
            nonlocal handle
            assert handle
            while True:
                # Mark it as started each loop because backoff could cause it to
                # be marked as unstarted
                handle._started = True
                try:
                    # We have to shield because we don't want the underlying
                    # result future to be cancelled
                    return await asyncio.shield(handle._result_fut)
                except _ActivityDoBackoffError as err:
                    # We have to sleep then reschedule. Note this sleep can be
                    # cancelled like any other timer.
                    await asyncio.sleep(
                        err.backoff.backoff_duration.ToTimedelta().total_seconds()
                    )
                    handle._apply_schedule_command(self._add_command(), err.backoff)
                    # We have to put the handle back on the pending activity
                    # dict with its new seq
                    self._pending_activities[handle._seq] = handle
                except asyncio.CancelledError:
                    # Send a cancel request to the activity
                    handle._apply_cancel_command(self._add_command())

        # Create the handle and set as pending
        handle = _ActivityHandle(self, input, run_activity())
        handle._apply_schedule_command(self._add_command())
        self._pending_activities[handle._seq] = handle
        return handle

    async def _outbound_signal_child_workflow(
        self, input: SignalChildWorkflowInput
    ) -> None:
        command = self._add_command()
        v = command.signal_external_workflow_execution
        v.child_workflow_id = input.child_workflow_id
        v.signal_name = input.signal
        if input.args:
            v.args.extend(self._payload_converter.to_payloads(input.args))
        if input.headers:
            temporalio.common._apply_headers(input.headers, v.headers)
        await self._signal_external_workflow(command)

    async def _outbound_signal_external_workflow(
        self, input: SignalExternalWorkflowInput
    ) -> None:
        command = self._add_command()
        v = command.signal_external_workflow_execution
        v.workflow_execution.namespace = input.namespace
        v.workflow_execution.workflow_id = input.workflow_id
        if input.workflow_run_id:
            v.workflow_execution.run_id = input.workflow_run_id
        v.signal_name = input.signal
        if input.args:
            v.args.extend(self._payload_converter.to_payloads(input.args))
        if input.headers:
            temporalio.common._apply_headers(input.headers, v.headers)
        await self._signal_external_workflow(command)

    async def _outbound_start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> _ChildWorkflowHandle:
        handle: Optional[_ChildWorkflowHandle] = None

        # Common code for handling cancel for start and run
        def apply_child_cancel_error() -> None:
            nonlocal handle
            assert handle
            # Send a cancel request to the child
            cancel_command = self._add_command()
            handle._apply_cancel_command(cancel_command)
            # If the cancel command is for external workflow, we
            # have to add a seq and mark it pending
            if cancel_command.HasField("request_cancel_external_workflow_execution"):
                cancel_seq = self._next_seq("external_cancel")
                cancel_command.request_cancel_external_workflow_execution.seq = (
                    cancel_seq
                )
                # TODO(cretz): Nothing waits on this future, so how
                # if at all should we report child-workflow cancel
                # request failure?
                self._pending_external_cancels[cancel_seq] = self.create_future()

        # Function that runs in the handle
        async def run_child() -> Any:
            nonlocal handle
            while True:
                assert handle
                try:
                    # We have to shield because we don't want the future itself
                    # to be cancelled
                    return await asyncio.shield(handle._result_fut)
                except asyncio.CancelledError:
                    apply_child_cancel_error()

        # Create the handle and set as pending
        handle = _ChildWorkflowHandle(
            self, self._next_seq("child_workflow"), input, run_child()
        )
        handle._apply_start_command(self._add_command())
        self._pending_child_workflows[handle._seq] = handle

        # Wait on start before returning
        while True:
            try:
                # We have to shield because we don't want the future itself
                # to be cancelled
                await asyncio.shield(handle._start_fut)
                return handle
            except asyncio.CancelledError:
                apply_child_cancel_error()

    #### Miscellaneous helpers ####
    # These are in alphabetical order.

    def _add_command(self) -> temporalio.bridge.proto.workflow_commands.WorkflowCommand:
        self._assert_not_read_only("add command")
        return self._current_completion.successful.commands.add()

    @contextmanager
    def _as_read_only(self) -> Iterator[None]:
        prev_val = self._read_only
        self._read_only = True
        try:
            yield None
        finally:
            self._read_only = prev_val

    def _assert_not_read_only(self, action_attempted: str) -> None:
        if self._read_only:
            raise temporalio.workflow.ReadOnlyContextError(
                f"While in read-only function, action attempted: {action_attempted}"
            )

    async def _cancel_external_workflow(
        self,
        # Should not have seq set
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        seq = self._next_seq("external_cancel")
        done_fut = self.create_future()
        command.request_cancel_external_workflow_execution.seq = seq

        # Set as pending
        self._pending_external_cancels[seq] = done_fut

        # Wait until done (there is no cancelling a cancel request)
        await done_fut

    def _check_condition(self, fn: Callable[[], bool], fut: asyncio.Future) -> bool:
        if fn():
            fut.set_result(True)
            return True
        return False

    def _convert_payloads(
        self,
        payloads: Sequence[temporalio.api.common.v1.Payload],
        types: Optional[List[Type]],
    ) -> List[Any]:
        if not payloads:
            return []
        # Only use type hints if they match count
        if types and len(types) != len(payloads):
            types = None
        try:
            return self._payload_converter.from_payloads(
                payloads,
                type_hints=types,
            )
        except temporalio.exceptions.FailureError:
            # Don't wrap payload conversion errors that would fail the workflow
            raise
        except Exception as err:
            raise RuntimeError("Failed decoding arguments") from err

    def _next_seq(self, type: str) -> int:
        seq = self._curr_seqs.get(type, 0) + 1
        self._curr_seqs[type] = seq
        return seq

    def _process_handler_args(
        self,
        job_name: str,
        job_input: Sequence[temporalio.api.common.v1.Payload],
        defn_name: Optional[str],
        defn_arg_types: Optional[List[Type]],
        defn_dynamic_vararg: bool,
    ) -> List[Any]:
        # If dynamic old-style vararg, args become name + varargs of given arg
        # types. If dynamic new-style raw value sequence, args become name +
        # seq of raw values.
        if not defn_name and defn_dynamic_vararg:
            # Take off the string type hint for conversion
            arg_types = defn_arg_types[1:] if defn_arg_types else None
            return [job_name] + self._convert_payloads(job_input, arg_types)
        if not defn_name:
            return [
                job_name,
                self._convert_payloads(
                    job_input, [temporalio.common.RawValue] * len(job_input)
                ),
            ]
        return self._convert_payloads(job_input, defn_arg_types)

    def _process_signal_job(
        self,
        defn: temporalio.workflow._SignalDefinition,
        job: temporalio.bridge.proto.workflow_activation.SignalWorkflow,
    ) -> None:
        try:
            args = self._process_handler_args(
                job.signal_name,
                job.input,
                defn.name,
                defn.arg_types,
                defn.dynamic_vararg,
            )
        except Exception:
            logger.exception(
                f"Failed deserializing signal input for {job.signal_name}, dropping the signal"
            )
            return
        input = HandleSignalInput(
            signal=job.signal_name, args=args, headers=job.headers
        )
        self.create_task(
            self._run_top_level_workflow_function(self._inbound.handle_signal(input)),
            name=f"signal: {job.signal_name}",
        )

    def _register_task(
        self,
        task: asyncio.Task,
        *,
        name: Optional[str],
    ) -> None:
        self._assert_not_read_only("create task")
        # Name not supported on older Python versions
        if sys.version_info >= (3, 8):
            # Put the workflow info at the end of the task name
            name = name or task.get_name()
            name += f" (workflow: {self._info.workflow_type}, id: {self._info.workflow_id}, run: {self._info.run_id})"
            task.set_name(name)
        # Add to and remove from our own non-weak set instead of relying on
        # Python's weak set which can collect these too early
        self._tasks.add(task)
        task.add_done_callback(self._tasks.remove)
        # When the workflow is GC'd (for whatever reason, e.g. eviction or
        # worker shutdown), still-open tasks get a message logged to the
        # exception handler about still pending. We disable this if the
        # attribute is available.
        if hasattr(task, "_log_destroy_pending"):
            setattr(task, "_log_destroy_pending", False)

    def _run_once(self, *, check_conditions: bool) -> None:
        try:
            asyncio._set_running_loop(self)

            # We instantiate the workflow class _inside_ here because __init__
            # needs to run with this event loop set
            if not self._object:
                self._object = self._defn.cls()

            # Run while there is anything ready
            while self._ready:
                # Run and remove all ready ones
                while self._ready:
                    handle = self._ready.popleft()
                    handle._run()

                    # Must throw here. Only really set inside
                    # _run_top_level_workflow_function.
                    if self._current_activation_error:
                        raise self._current_activation_error

                # Check conditions which may add to the ready list. Also remove
                # conditions whose futures have already cancelled (e.g. when
                # timed out).
                if check_conditions:
                    self._conditions[:] = [
                        t
                        for t in self._conditions
                        if not t[1].done() and not self._check_condition(*t)
                    ]
        finally:
            asyncio._set_running_loop(None)

    # This is used for the primary workflow function and signal handlers in
    # order to apply common exception handling to each
    async def _run_top_level_workflow_function(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except _ContinueAsNewError as err:
            logger.debug("Workflow requested continue as new")
            err._apply_command(self._add_command())

        # Note in some Python versions, cancelled error does not extend
        # exception
        # TODO(cretz): Should I fail the task on BaseException too (e.g.
        # KeyboardInterrupt)?
        except (Exception, asyncio.CancelledError) as err:
            logger.debug(
                f"Workflow raised failure with run ID {self._info.run_id}",
                exc_info=True,
            )

            # All asyncio cancelled errors become Temporal cancelled errors
            if isinstance(err, asyncio.CancelledError):
                err = temporalio.exceptions.CancelledError(str(err))

            # If a cancel was ever requested and this is a cancellation, or an
            # activity/child cancellation, we add a cancel command. Technically
            # this means that a swallowed cancel followed by, say, an activity
            # cancel later on will show the workflow as cancelled. But this is
            # a Temporal limitation in that cancellation is a state not an
            # event.
            if self._cancel_requested and temporalio.exceptions.is_cancelled_exception(
                err
            ):
                self._add_command().cancel_workflow_execution.SetInParent()
            elif isinstance(err, temporalio.exceptions.FailureError):
                # All other failure errors fail the workflow
                self._set_workflow_failure(err)
            else:
                # All other exceptions fail the task
                self._current_activation_error = err
        except BaseException as err:
            # During tear down, generator exit and no-runtime exceptions can appear
            if not self._deleting:
                raise
            if not isinstance(
                err, (GeneratorExit, temporalio.workflow._NotInWorkflowEventLoopError)
            ):
                logger.debug(
                    "Ignoring exception while deleting workflow", exc_info=True
                )

    def _set_workflow_failure(self, err: temporalio.exceptions.FailureError) -> None:
        # All other failure errors fail the workflow
        failure = self._add_command().fail_workflow_execution.failure
        failure.SetInParent()
        try:
            self._failure_converter.to_failure(err, self._payload_converter, failure)
        except Exception as inner_err:
            raise ValueError("Failed converting workflow exception") from inner_err

    async def _signal_external_workflow(
        self,
        # Should not have seq set
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        seq = self._next_seq("external_signal")
        done_fut = self.create_future()
        command.signal_external_workflow_execution.seq = seq

        # Set as pending
        self._pending_external_signals[seq] = done_fut

        # Wait until completed or cancelled
        while True:
            try:
                # We have to shield because we don't want the future itself
                # to be cancelled
                return await asyncio.shield(done_fut)
            except asyncio.CancelledError:
                cancel_command = self._add_command()
                cancel_command.cancel_signal_workflow.seq = seq

    def _stack_trace(self) -> str:
        stacks = []
        for task in self._tasks:
            # TODO(cretz): These stacks are not very clean currently
            frames = []
            for frame in task.get_stack():
                frames.append(
                    traceback.FrameSummary(
                        frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name
                    )
                )
            stacks.append(
                f"Stack for {task!r} (most recent call last):\n"
                + "\n".join(traceback.format_list(frames))
            )
        return "\n\n".join(stacks)

    #### asyncio.AbstractEventLoop function impls ####
    # These are in the order defined in CPython's impl of the base class. Many
    # functions are intentionally not implemented/supported.

    def _timer_handle_cancelled(self, handle: asyncio.TimerHandle) -> None:
        if not isinstance(handle, _TimerHandle):
            raise TypeError("Expected Temporal timer handle")
        if not self._pending_timers.pop(handle._seq, None):
            return
        handle._apply_cancel_command(self._add_command())

    def call_soon(
        self,
        callback: Callable[..., Any],
        *args: Any,
        context: Optional[contextvars.Context] = None,
    ) -> asyncio.Handle:
        self._assert_not_read_only("schedule task")
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
        self._assert_not_read_only("schedule timer")
        # Delay must be positive
        if delay < 0:
            raise RuntimeError("Attempting to schedule timer with negative delay")

        # Create, schedule, and return
        seq = self._next_seq("timer")
        handle = _TimerHandle(seq, self.time() + delay, callback, args, self, context)
        handle._apply_start_command(self._add_command(), delay)
        self._pending_timers[seq] = handle
        return handle

    def time(self) -> float:
        return self._time_ns / 1e9

    def create_future(self) -> asyncio.Future[Any]:
        return asyncio.Future(loop=self)

    def create_task(
        self,
        coro: Union[Awaitable[_T], Generator[Any, None, _T]],
        *,
        name: Optional[str] = None,
        context: Optional[contextvars.Context] = None,
    ) -> asyncio.Task[_T]:
        # Context only supported on newer Python versions
        if sys.version_info >= (3, 11):
            task = asyncio.Task(coro, loop=self, context=context)  # type: ignore
        else:
            task = asyncio.Task(coro, loop=self)
        self._register_task(task, name=name)
        return task

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
        handler = self._instance.workflow_get_signal_handler(
            input.signal
        ) or self._instance.workflow_get_signal_handler(None)
        # Handler should always be present at this point
        assert handler
        if inspect.iscoroutinefunction(handler):
            await handler(*input.args)
        else:
            handler(*input.args)

    async def handle_query(self, input: HandleQueryInput) -> Any:
        handler = self._instance.workflow_get_query_handler(
            input.query
        ) or self._instance.workflow_get_query_handler(None)
        # Handler should always be present at this point
        assert handler
        if inspect.iscoroutinefunction(handler):
            return await handler(*input.args)
        else:
            return handler(*input.args)

    def handle_update_validator(self, input: HandleUpdateInput) -> None:
        # Do not "or None" the validator, since we only want to use the validator for
        # the specific named update - we shouldn't fall back to the dynamic validator
        # for some defined, named update which doesn't have a defined validator.
        handler = self._instance.workflow_get_update_validator(input.update)
        # Validator may not be defined
        if handler is not None:
            handler(*input.args)

    async def handle_update_handler(self, input: HandleUpdateInput) -> Any:
        handler = self._instance.workflow_get_update_handler(
            input.update
        ) or self._instance.workflow_get_update_handler(None)
        # Handler should always be present at this point
        assert handler
        if inspect.iscoroutinefunction(handler):
            return await handler(*input.args)
        else:
            return handler(*input.args)


class _WorkflowOutboundImpl(WorkflowOutboundInterceptor):
    def __init__(self, instance: _WorkflowInstanceImpl) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._instance = instance

    def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        self._instance._outbound_continue_as_new(input)

    def info(self) -> temporalio.workflow.Info:
        return self._instance._info

    async def signal_child_workflow(self, input: SignalChildWorkflowInput) -> None:
        return await self._instance._outbound_signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: SignalExternalWorkflowInput
    ) -> None:
        await self._instance._outbound_signal_external_workflow(input)

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

    def _apply_start_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
        delay: float,
    ) -> None:
        command.start_timer.seq = self._seq
        command.start_timer.start_to_fire_timeout.FromNanoseconds(int(delay * 1e9))

    def _apply_cancel_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        command.cancel_timer.seq = self._seq


class _ActivityDoBackoffError(BaseException):
    def __init__(
        self, backoff: temporalio.bridge.proto.activity_result.DoBackoff
    ) -> None:
        super().__init__("Backoff")
        self.backoff = backoff


class _ActivityHandle(temporalio.workflow.ActivityHandle[Any]):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        input: Union[StartActivityInput, StartLocalActivityInput],
        fn: Awaitable[Any],
    ) -> None:
        super().__init__(fn)
        self._instance = instance
        self._seq = instance._next_seq("activity")
        self._input = input
        self._result_fut = instance.create_future()
        self._started = False
        instance._register_task(self, name=f"activity: {input.activity}")

    def cancel(self, msg: Optional[Any] = None) -> bool:
        self._instance._assert_not_read_only("cancel activity handle")
        # We override this because if it's not yet started and not done, we need
        # to send a cancel command because the async function won't run to trap
        # the cancel (i.e. cancelled before started)
        if not self._started and not self.done():
            self._apply_cancel_command(self._instance._add_command())
        # Message not supported in older versions
        if sys.version_info < (3, 9):
            return super().cancel()
        return super().cancel(msg)

    def _resolve_success(self, result: Any) -> None:
        # We intentionally let this error if already done
        self._result_fut.set_result(result)

    def _resolve_failure(self, err: BaseException) -> None:
        # If it was never started, we don't need to set this failure. In cases
        # where this is cancelled before started, setting this exception causes
        # a Python warning to be emitted because this future is never awaited
        # on.
        if self._started:
            self._result_fut.set_exception(err)

    def _resolve_backoff(
        self, backoff: temporalio.bridge.proto.activity_result.DoBackoff
    ) -> None:
        # Change the sequence and set backoff exception on the current future
        self._seq = self._instance._next_seq("activity")
        self._result_fut.set_exception(_ActivityDoBackoffError(backoff))
        # Replace the result future since that one is resolved now
        self._result_fut = self._instance.create_future()
        # Mark this as not started since it's a new future
        self._started = False

    def _apply_schedule_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
        local_backoff: Optional[
            temporalio.bridge.proto.activity_result.DoBackoff
        ] = None,
    ) -> None:
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
        if self._input.headers:
            temporalio.common._apply_headers(self._input.headers, v.headers)
        if self._input.args:
            v.arguments.extend(
                self._instance._payload_converter.to_payloads(self._input.args)
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
            self._input.retry_policy.apply_to_proto(v.retry_policy)
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
            command.schedule_activity.do_not_eagerly_execute = (
                self._input.disable_eager_execution
            )
            if self._input.versioning_intent:
                command.schedule_activity.versioning_intent = (
                    self._input.versioning_intent._to_proto()
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

    def _apply_cancel_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        if isinstance(self._input, StartActivityInput):
            command.request_cancel_activity.seq = self._seq
        else:
            command.request_cancel_local_activity.seq = self._seq


class _ChildWorkflowHandle(temporalio.workflow.ChildWorkflowHandle[Any, Any]):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        seq: int,
        input: StartChildWorkflowInput,
        fn: Awaitable[Any],
    ) -> None:
        super().__init__(fn)
        self._instance = instance
        self._seq = seq
        self._input = input
        self._start_fut: asyncio.Future[None] = instance.create_future()
        self._result_fut: asyncio.Future[Any] = instance.create_future()
        self._first_execution_run_id = "<unknown>"
        instance._register_task(self, name=f"child: {input.workflow}")

    @property
    def id(self) -> str:
        return self._input.id

    @property
    def first_execution_run_id(self) -> Optional[str]:
        return self._first_execution_run_id

    async def signal(
        self,
        signal: Union[str, Callable],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        self._instance._assert_not_read_only("signal child handle")
        await self._instance._outbound.signal_child_workflow(
            SignalChildWorkflowInput(
                signal=temporalio.workflow._SignalDefinition.must_name_from_fn_or_str(
                    signal
                ),
                args=temporalio.common._arg_or_args(arg, args),
                child_workflow_id=self._input.id,
                headers={},
            )
        )

    def _resolve_start_success(self, run_id: str) -> None:
        self._first_execution_run_id = run_id
        # We intentionally let this error if already done
        self._start_fut.set_result(None)

    def _resolve_success(self, result: Any) -> None:
        # We intentionally let this error if already done
        self._result_fut.set_result(result)

    def _resolve_failure(self, err: BaseException) -> None:
        if self._start_fut.done():
            # We intentionally let this error if already done
            self._result_fut.set_exception(err)
        else:
            self._start_fut.set_exception(err)
            # Set the result as none to avoid Python warning about unhandled
            # future
            self._result_fut.set_result(None)

    def _apply_start_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        v = command.start_child_workflow_execution
        v.seq = self._seq
        v.namespace = self._instance._info.namespace
        v.workflow_id = self._input.id
        v.workflow_type = self._input.workflow
        v.task_queue = self._input.task_queue or self._instance._info.task_queue
        if self._input.args:
            v.input.extend(
                self._instance._payload_converter.to_payloads(self._input.args)
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
            "temporalio.api.enums.v1.WorkflowIdReusePolicy.ValueType",
            int(self._input.id_reuse_policy),
        )
        if self._input.retry_policy:
            self._input.retry_policy.apply_to_proto(v.retry_policy)
        v.cron_schedule = self._input.cron_schedule
        if self._input.headers:
            temporalio.common._apply_headers(self._input.headers, v.headers)
        if self._input.memo:
            for k, val in self._input.memo.items():
                v.memo[k].CopyFrom(
                    self._instance._payload_converter.to_payloads([val])[0]
                )
        if self._input.search_attributes:
            _encode_search_attributes(
                self._input.search_attributes, v.search_attributes
            )
        v.cancellation_type = cast(
            "temporalio.bridge.proto.child_workflow.ChildWorkflowCancellationType.ValueType",
            int(self._input.cancellation_type),
        )
        if self._input.versioning_intent:
            v.versioning_intent = self._input.versioning_intent._to_proto()

    # If request cancel external, result does _not_ have seq
    def _apply_cancel_command(
        self,
        command: temporalio.bridge.proto.workflow_commands.WorkflowCommand,
    ) -> None:
        command.cancel_child_workflow_execution.child_workflow_seq = self._seq


class _ExternalWorkflowHandle(temporalio.workflow.ExternalWorkflowHandle[Any]):
    def __init__(
        self,
        instance: _WorkflowInstanceImpl,
        id: str,
        run_id: Optional[str],
    ) -> None:
        super().__init__()
        self._instance = instance
        self._id = id
        self._run_id = run_id

    @property
    def id(self) -> str:
        return self._id

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    async def signal(
        self,
        signal: Union[str, Callable],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
    ) -> None:
        self._instance._assert_not_read_only("signal external handle")
        await self._instance._outbound.signal_external_workflow(
            SignalExternalWorkflowInput(
                signal=temporalio.workflow._SignalDefinition.must_name_from_fn_or_str(
                    signal
                ),
                args=temporalio.common._arg_or_args(arg, args),
                namespace=self._instance._info.namespace,
                workflow_id=self._id,
                workflow_run_id=self._run_id,
                headers={},
            )
        )

    async def cancel(self) -> None:
        self._instance._assert_not_read_only("cancel external handle")
        command = self._instance._add_command()
        v = command.request_cancel_external_workflow_execution
        v.workflow_execution.namespace = self._instance._info.namespace
        v.workflow_execution.workflow_id = self._id
        if self._run_id:
            v.workflow_execution.run_id = self._run_id
        await self._instance._cancel_external_workflow(command)


class _ContinueAsNewError(temporalio.workflow.ContinueAsNewError):
    def __init__(
        self, instance: _WorkflowInstanceImpl, input: ContinueAsNewInput
    ) -> None:
        super().__init__("Continue as new")
        self._instance = instance
        self._input = input

    def _apply_command(
        self, command: temporalio.bridge.proto.workflow_commands.WorkflowCommand
    ) -> None:
        v = command.continue_as_new_workflow_execution
        v.SetInParent()
        if self._input.workflow:
            v.workflow_type = self._input.workflow
        if self._input.task_queue:
            v.task_queue = self._input.task_queue
        if self._input.args:
            v.arguments.extend(
                self._instance._payload_converter.to_payloads(self._input.args)
            )
        if self._input.run_timeout:
            v.workflow_run_timeout.FromTimedelta(self._input.run_timeout)
        if self._input.task_timeout:
            v.workflow_task_timeout.FromTimedelta(self._input.task_timeout)
        if self._input.headers:
            temporalio.common._apply_headers(self._input.headers, v.headers)
        if self._input.retry_policy:
            self._input.retry_policy.apply_to_proto(v.retry_policy)
        if self._input.memo:
            for k, val in self._input.memo.items():
                v.memo[k].CopyFrom(
                    self._instance._payload_converter.to_payloads([val])[0]
                )
        if self._input.search_attributes:
            _encode_search_attributes(
                self._input.search_attributes, v.search_attributes
            )
        if self._input.versioning_intent:
            v.versioning_intent = self._input.versioning_intent._to_proto()


def _encode_search_attributes(
    attributes: temporalio.common.SearchAttributes,
    payloads: Mapping[str, temporalio.api.common.v1.Payload],
) -> None:
    """Encode search attributes as bridge payloads."""
    for k, vals in attributes.items():
        payloads[k].CopyFrom(temporalio.converter.encode_search_attribute_values(vals))


class _WorkflowExternFunctions(TypedDict):
    __temporal_get_metric_meter: Callable[[], temporalio.common.MetricMeter]


class _ReplaySafeMetricMeter(temporalio.common.MetricMeter):
    def __init__(self, underlying: temporalio.common.MetricMeter) -> None:
        self._underlying = underlying

    def create_counter(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricCounter:
        return _ReplaySafeMetricCounter(
            self._underlying.create_counter(name, description, unit)
        )

    def create_histogram(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricHistogram:
        return _ReplaySafeMetricHistogram(
            self._underlying.create_histogram(name, description, unit)
        )

    def create_gauge(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricGauge:
        return _ReplaySafeMetricGauge(
            self._underlying.create_gauge(name, description, unit)
        )

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricMeter:
        return _ReplaySafeMetricMeter(
            self._underlying.with_additional_attributes(additional_attributes)
        )


class _ReplaySafeMetricCounter(temporalio.common.MetricCounter):
    def __init__(self, underlying: temporalio.common.MetricCounter) -> None:
        self._underlying = underlying

    @property
    def name(self) -> str:
        return self._underlying.name

    @property
    def description(self) -> Optional[str]:
        return self._underlying.description

    @property
    def unit(self) -> Optional[str]:
        return self._underlying.unit

    def add(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if not temporalio.workflow.unsafe.is_replaying():
            self._underlying.add(value, additional_attributes)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricCounter:
        return _ReplaySafeMetricCounter(
            self._underlying.with_additional_attributes(additional_attributes)
        )


class _ReplaySafeMetricHistogram(temporalio.common.MetricHistogram):
    def __init__(self, underlying: temporalio.common.MetricHistogram) -> None:
        self._underlying = underlying

    @property
    def name(self) -> str:
        return self._underlying.name

    @property
    def description(self) -> Optional[str]:
        return self._underlying.description

    @property
    def unit(self) -> Optional[str]:
        return self._underlying.unit

    def record(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if not temporalio.workflow.unsafe.is_replaying():
            self._underlying.record(value, additional_attributes)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricHistogram:
        return _ReplaySafeMetricHistogram(
            self._underlying.with_additional_attributes(additional_attributes)
        )


class _ReplaySafeMetricGauge(temporalio.common.MetricGauge):
    def __init__(self, underlying: temporalio.common.MetricGauge) -> None:
        self._underlying = underlying

    @property
    def name(self) -> str:
        return self._underlying.name

    @property
    def description(self) -> Optional[str]:
        return self._underlying.description

    @property
    def unit(self) -> Optional[str]:
        return self._underlying.unit

    def set(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if not temporalio.workflow.unsafe.is_replaying():
            self._underlying.set(value, additional_attributes)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricGauge:
        return _ReplaySafeMetricGauge(
            self._underlying.with_additional_attributes(additional_attributes)
        )
