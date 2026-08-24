"""Datadog tracing interceptors for Temporal workflow inbound and outbound calls."""

import contextvars
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn, cast

import temporalio.worker
import temporalio.workflow
from temporalio.contrib.datadog._constants import (
    COMMON_ATTRIBUTE_MAP,
    OperationNames,
    SpanAttributes,
)
from temporalio.contrib.datadog._id_generator import gen_span_id
from temporalio.contrib.datadog._propagator import _Propagator

# ContextVar keeps this flag task-local. Sandboxed workflows load
# non-passthrough modules into a per-instance module namespace; unsandboxed
# workflow tasks capture their own context.
_trace_disconnected: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_trace_disconnected", default=False
)

# Live RunWorkflow span for the current execution, set before user code runs.
# Never None — RunWorkflow is exempt from the replay guard in span_ctx.
# Isolated per execution by the same mechanism as _trace_disconnected.
_active_workflow_span: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_active_workflow_span", default=None
)

# Holds (trace_id, span_id) for the active RunWorkflow span so that the log
# filter below can inject dd.trace_id / dd.span_id into every workflow.logger
# call without activating the span (which is unsafe inside the sandbox).
_current_span_info: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("_current_span_info", default=None)
)


class _DDTraceLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        info = _current_span_info.get()
        if info is not None:
            record.__dict__["dd.trace_id"] = info[0]
            record.__dict__["dd.span_id"] = info[1]
        return True


# Each sandbox instance can import this module again; avoid adding the same
# filter repeatedly to Temporal's shared workflow logger.
if not getattr(
    temporalio.workflow.logger.base_logger, "_dd_trace_filter_installed", False
):
    temporalio.workflow.logger.base_logger.addFilter(_DDTraceLogFilter())
    temporalio.workflow.logger.base_logger._dd_trace_filter_installed = True  # type: ignore[attr-defined]


@dataclass
class WorkflowTracingConfig:
    # Set the relevant flag to suppress signal, query, or update spans while
    # keeping workflow and activity tracing enabled.
    disable_signal_tracing: bool
    disable_query_tracing: bool
    disable_update_tracing: bool

    @staticmethod
    def default_config() -> "WorkflowTracingConfig":
        return WorkflowTracingConfig(
            disable_signal_tracing=False,
            disable_query_tracing=False,
            disable_update_tracing=False,
        )


class DatadogTracingWorkflowInboundInterceptor(
    temporalio.worker.WorkflowInboundInterceptor
):
    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self._span_counter = 1  # Reserve the 1 for the RunWorkflow span

        externs = temporalio.workflow.extern_functions()
        self._start_span_extern = cast(
            Callable[
                [str, str, dict[str, Any], Any | None, str | None, int | None], Any
            ],
            externs["__temporal_datadog_start_sandboxed_span"],
        )
        self._finish_span_extern = cast(
            Callable[[str, Any | None, BaseException | None], None],
            externs["__temporal_datadog_finish_sandboxed_span"],
        )
        config_func = cast(
            Callable[[], tuple[_Propagator, WorkflowTracingConfig]],
            externs["__temporal_datadog_configure_workflow_tracing"],
        )
        self.propagator, self.config = config_func()

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(_WorkflowOutboundInterceptor(outbound, self))

    @contextmanager
    def span_ctx(
        self,
        operation_name: str,
        resource_name: str,
        input: Any,
        idempotency_key: str | None = None,
        start_time: int | None = None,
    ) -> Generator[tuple[Any, Any], None, None]:
        attributes = self._get_span_attributes(input)
        parent_ctx = self._parent_ctx_for(operation_name, input)

        # Idempotency-keyed HandleSignal and HandleUpdate spans are suppressed
        # while replaying. RunWorkflow is exempt so a restarted worker recreates
        # its long-lived span. HandleQuery and ValidateUpdate pass no idempotency
        # key, so this guard does not suppress them.
        if (
            idempotency_key is not None
            and operation_name != OperationNames.RUN_WORKFLOW
            and temporalio.workflow.unsafe.is_replaying()
        ):
            span = None
        else:
            span = self._start_span_extern(
                operation_name,
                resource_name,
                attributes,
                parent_ctx,
                idempotency_key,
                start_time,
            )
        exc: BaseException | None = None
        try:
            yield input, span
        except BaseException as e:
            exc = e
            raise
        finally:
            self._finish_span_extern(operation_name, span, exc)

    def _parent_ctx_for(self, operation_name: str, input: Any) -> Any:
        # Parent from workflow header
        if operation_name == OperationNames.RUN_WORKFLOW:
            return self.propagator.extract_headers(temporalio.workflow.info().headers)

        # Parent from input headers
        ctx = self.propagator.extract_headers(input.headers)
        if ctx is not None:
            return ctx

        # Make RunWorkflow the parent
        return self.recover_workflow_span()

    def recover_workflow_span(self) -> Any:
        # Reconstruct the RunWorkflow context from the workflow start headers by
        # reusing the trace_id from StartWorkflow and overriding the span_id with
        # RunWorkflow's deterministic ID. Reliable across sandbox task boundaries
        # because workflow.info().headers is always available.
        ctx = self.propagator.extract_headers(temporalio.workflow.info().headers)
        if ctx is not None:
            ctx.span_id = gen_span_id(self._make_idempotency_key(1))
            return ctx

        # No start headers (uninstrumented client). Fall back to the live span.
        span = span_from_workflow_context()
        return span.context if span is not None else None

    def _get_span_attributes(self, input: Any) -> dict[str, Any]:
        info = temporalio.workflow.info()
        attrs: dict[str, Any] = {
            SpanAttributes.WORKFLOW_ID: info.workflow_id,
            SpanAttributes.RUN_ID: info.run_id,
            SpanAttributes.WORKFLOW_TYPE: info.workflow_type,
        }
        for field, span_key in COMMON_ATTRIBUTE_MAP:
            if val := getattr(input, field, None):
                attrs[span_key] = val
        if getattr(input, "update", None):
            attrs[SpanAttributes.UPDATE_NAME] = input.update
            if getattr(input, "id", None):
                attrs[SpanAttributes.UPDATE_ID] = input.id
        if getattr(input, "workflow", None):
            attrs[SpanAttributes.CHILD_WORKFLOW_TYPE] = input.workflow
            if getattr(input, "id", None):
                attrs[SpanAttributes.CHILD_WORKFLOW_ID] = input.id
        if isinstance(input, temporalio.worker.StartLocalActivityInput):
            attrs[SpanAttributes.LOCAL] = True
        return attrs

    def _make_idempotency_key(self, counter: int) -> str:
        info = temporalio.workflow.info()
        # Matches the Go SDK's idempotency key
        return f"WorkflowInboundInterceptor:{info.namespace}:{info.workflow_id}:{info.run_id}:{counter}"

    def _next_idempotency_key(self) -> str:
        self._span_counter += 1
        return self._make_idempotency_key(self._span_counter)

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        info = temporalio.workflow.info()
        with self.span_ctx(
            OperationNames.RUN_WORKFLOW,
            info.workflow_type,
            input,
            idempotency_key=self._make_idempotency_key(1),
            start_time=int(info.workflow_start_time.timestamp() * 1e9),
        ) as (i, span):
            if span is not None:
                i.headers = self.propagator.inject_headers(i.headers, span.context)
                tid = span.context.trace_id
                # Match ddtrace's format_trace_id: decimal for 64-bit IDs, 32-char hex for 128-bit.
                formatted_tid = f"{tid:032x}" if tid > (1 << 64) - 1 else str(tid)
                _current_span_info.set((formatted_tid, str(span.span_id)))
            _active_workflow_span.set(span)
            return await super().execute_workflow(i)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        if self.config.disable_signal_tracing:
            return await super().handle_signal(input)
        with self.span_ctx(
            OperationNames.HANDLE_SIGNAL,
            input.signal,
            input,
            self._next_idempotency_key(),
        ) as (i, _):
            await super().handle_signal(i)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        if self.config.disable_query_tracing:
            return await super().handle_query(input)
        with self.span_ctx(OperationNames.HANDLE_QUERY, input.query, input) as (i, _):
            return await super().handle_query(i)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        if self.config.disable_update_tracing:
            return super().handle_update_validator(input)
        with self.span_ctx(OperationNames.VALIDATE_UPDATE, input.update, input) as (
            i,
            _,
        ):
            super().handle_update_validator(i)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        if self.config.disable_update_tracing:
            return await super().handle_update_handler(input)
        with self.span_ctx(
            OperationNames.HANDLE_UPDATE,
            input.update,
            input,
            self._next_idempotency_key(),
        ) as (i, _):
            return await super().handle_update_handler(i)


class _WorkflowOutboundInterceptor(temporalio.worker.WorkflowOutboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        root: DatadogTracingWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        if not _trace_disconnected.get():
            input.headers = self.root.propagator.inject_headers(
                input.headers, self.root.recover_workflow_span()
            )
        super().continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        if self.root.config.disable_signal_tracing:
            return await super().signal_child_workflow(input)
        if temporalio.workflow.unsafe.is_replaying():
            return await super().signal_child_workflow(input)
        with self.root.span_ctx(
            OperationNames.SIGNAL_CHILD_WORKFLOW, input.signal, input
        ) as (i, span):
            if span is not None:
                i.headers = self.root.propagator.inject_headers(i.headers, span.context)
            await super().signal_child_workflow(i)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        if self.root.config.disable_signal_tracing:
            return await super().signal_external_workflow(input)
        if temporalio.workflow.unsafe.is_replaying():
            return await super().signal_external_workflow(input)
        with self.root.span_ctx(
            OperationNames.SIGNAL_EXTERNAL_WORKFLOW, input.signal, input
        ) as (i, span):
            if span is not None:
                i.headers = self.root.propagator.inject_headers(i.headers, span.context)
            await super().signal_external_workflow(i)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        if temporalio.workflow.unsafe.is_replaying():
            return super().start_activity(input)
        with self.root.span_ctx(
            OperationNames.START_ACTIVITY, input.activity, input
        ) as (i, span):
            if span is not None:
                i.headers = self.root.propagator.inject_headers(i.headers, span.context)
            return super().start_activity(i)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        if temporalio.workflow.unsafe.is_replaying():
            return await super().start_child_workflow(input)
        with self.root.span_ctx(
            OperationNames.START_CHILD_WORKFLOW, input.workflow, input
        ) as (i, span):
            if span is not None:
                i.headers = self.root.propagator.inject_headers(i.headers, span.context)
            return await super().start_child_workflow(i)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        if temporalio.workflow.unsafe.is_replaying():
            return super().start_local_activity(input)
        with self.root.span_ctx(
            OperationNames.START_ACTIVITY, input.activity, input
        ) as (i, span):
            if span is not None:
                i.headers = self.root.propagator.inject_headers(i.headers, span.context)
            return super().start_local_activity(i)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        # Skip Nexus tracing during workflow replay so replay does not emit a
        # duplicate StartNexusOperation span.
        if temporalio.workflow.unsafe.is_replaying():
            return await super().start_nexus_operation(input)

        with self.root.span_ctx(
            OperationNames.START_NEXUS_OPERATION,
            f"{input.service}/{input.operation_name}",
            input,
        ) as (i, span):
            if span is not None:
                # Nexus uses plain string headers, not Temporal payload headers.
                carrier = self.root.propagator.inject(span.context)
                i.headers = {**(i.headers or {}), **carrier}
            return await super().start_nexus_operation(i)


def span_from_workflow_context() -> Any:
    """Return the active RunWorkflow ddtrace span for this execution.

    Always returns a live span, including during replay on a new worker, so
    custom tags set here survive a worker restart::

        span = span_from_workflow_context()
        if span is not None:
            span.set_tag("my.tag", value)

    Python equivalent of the Go SDK's ``SpanFromWorkflowContext``.  Unlike the
    Go version, which takes a ``workflow.Context`` and can return any
    operation's span, this always returns the RunWorkflow span.
    """
    # NOTE: fork-specific workaround; reconsider if this integration is ever proposed upstream.
    # Prefer the extern so that the call reaches the host-side ContextVar even
    # when the workflow sandbox has reimported this module into its own namespace.
    fn = temporalio.workflow.extern_functions().get(
        "__temporal_datadog_get_workflow_span"
    )
    if fn is not None:
        return fn()
    return _active_workflow_span.get()


def disconnect_trace_span_from_workflow_context() -> None:
    """Prevent the current trace from propagating into the next ContinueAsNew execution.

    Call before ``workflow.continue_as_new()``; the next run starts a fresh
    root span rather than continuing this trace::

        disconnect_trace_span_from_workflow_context()
        workflow.continue_as_new(count + 1)

    """
    _trace_disconnected.set(True)
    # NOTE: fork-specific workaround; reconsider if this integration is ever proposed upstream.
    # Call through the extern so that the flag is set on the host-side ContextVar
    # where _WorkflowOutboundInterceptor.continue_as_new reads it.
    fn = temporalio.workflow.extern_functions().get(
        "__temporal_datadog_set_trace_disconnected"
    )
    if fn is not None:
        fn()
