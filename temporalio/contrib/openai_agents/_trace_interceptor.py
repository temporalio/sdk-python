"""Adds OpenAI Agents traces and spans to Temporal workflows and activities."""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from agents import CustomSpanData, custom_span, get_current_span, trace
from agents.tracing import (
    get_trace_provider,
)
from agents.tracing.scope import Scope
from agents.tracing.spans import Span

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from temporalio import activity, workflow

HEADER_KEY = "__openai_span"


class _InputWithHeaders(Protocol):
    headers: Mapping[str, temporalio.api.common.v1.Payload]


def set_header_from_context(input: _InputWithHeaders) -> None:
    """Inserts the OpenAI Agents trace/span data in the input header."""
    current = get_current_span()
    trace = get_trace_provider().get_current_trace()
    otel_span = getattr(trace, "__otel_span", None)
    input.headers = {
        **input.headers,
        HEADER_KEY: temporalio.converter.PayloadConverter.default.to_payload(
            {
                "traceName": trace.name if trace else "Unknown Workflow",
                "spanId": current.span_id if current else None,
                "traceId": trace.trace_id if trace else None,
                "otelTraceId": otel_span.get_span_context().trace_id
                if otel_span
                else None,
                "otelSpanId": otel_span.get_span_context().span_id
                if otel_span
                else None,
            }
        ),
    }


@contextmanager
def context_from_header(
    input: _InputWithHeaders,
    payload_converter: temporalio.converter.PayloadConverter,
    start_trace: bool = False,
):
    """Extracts and initializes trace information the input header."""
    payload = input.headers.get(HEADER_KEY)
    span_info = payload_converter.from_payload(payload) if payload else None
    if span_info is None:
        yield
    else:
        workflow_type = (
            activity.info().workflow_type
            if activity.in_activity()
            else workflow.info().workflow_type
        )
        current_trace = get_trace_provider().get_current_trace()
        if current_trace is None and span_info["traceId"] is not None:
            metadata = {
                "temporal:workflowId": activity.info().workflow_id
                if activity.in_activity()
                else workflow.info().workflow_id,
                "temporal:runId": activity.info().workflow_run_id
                if activity.in_activity()
                else workflow.info().run_id,
                "temporal:workflowType": workflow_type,
            }
            current_trace = trace(
                span_info["traceName"],
                trace_id=span_info["traceId"],
                metadata=metadata,
            )

            if start_trace:
                current_trace.start(mark_as_current=True)
            else:
                Scope.set_current_trace(current_trace)

        current_span = get_trace_provider().get_current_span()
        if current_span is None and span_info["spanId"] is not None:
            current_span = get_trace_provider().create_span(
                span_data=CustomSpanData(name="", data={}), span_id=span_info["spanId"]
            )
            if start_trace:
                current_span.start(mark_as_current=True)
            else:
                Scope.set_current_span(current_span)

        yield


@contextmanager
def temporal_span(
    add_temporal_spans: bool,
    span_name: str,
):
    """Create a temporal span context manager.

    Args:
        add_temporal_spans: Whether to add temporal-specific span data.
        span_name: The name of the span to create.

    Yields:
        A span context with temporal metadata if enabled.
    """
    if add_temporal_spans:
        """Extracts and initializes trace information the input header."""
        data = (
            {
                "activityId": activity.info().activity_id,
                "activity": activity.info().activity_type,
            }
            if activity.in_activity()
            else None
        )
        current_span = get_trace_provider().get_current_span()

        with custom_span(name=span_name, parent=current_span, data=data):
            yield
    else:
        yield


class RunIdRandom:
    """Random uuid generator seeded by the run id of the workflow.
    Doesn't currently support replay over reset correctly.
    """

    def __init__(self):
        """Create a new random UUID generator."""
        self._random = random.Random("OpenAIPlugin" + workflow.info().run_id)

    def uuid4(self) -> str:
        """Generate a random UUID."""
        return uuid.UUID(
            bytes=random.getrandbits(16 * 8).to_bytes(16, "big"), version=4
        ).hex[:24]


def _ensure_tracing_random() -> None:
    """We use a custom uuid generator for spans to ensure that changes to user code workflow.random usage
    do not affect tracing and vice versa.
    """
    instance = workflow.instance()
    if not hasattr(instance, "__temporal_openai_tracing_random"):
        setattr(
            workflow.instance(),
            "__temporal_openai_tracing_random",
            RunIdRandom(),
        )


class OpenAIAgentsContextPropagationInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    """Interceptor that propagates OpenAI agent tracing context through Temporal workflows and activities.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    This interceptor enables tracing of OpenAI agent operations across Temporal workflows
    and activities. It propagates trace context through workflow and activity boundaries,
    allowing for end-to-end tracing of agent operations.

    The interceptor handles:
    1. Propagating trace context from client to workflow
    2. Propagating trace context from workflow to activities
    3. Maintaining trace context across workflow and activity boundaries

    Example usage:
        interceptor = OpenAIAgentsTracingInterceptor()
        client = await Client.connect("localhost:7233", interceptors=[interceptor])
        worker = Worker(client, task_queue="my-task-queue", interceptors=[interceptor])
    """

    def __init__(
        self,
        payload_converter: temporalio.converter.PayloadConverter = temporalio.converter.default().payload_converter,
        add_temporal_spans: bool = True,
        start_traces: bool = False,
    ) -> None:
        """Initialize the interceptor with a payload converter.

        Args:
            payload_converter: The payload converter to use for serializing/deserializing
                trace context. Defaults to the default Temporal payload converter.
            add_temporal_spans: Whether to add temporal-specific spans to traces.
            start_traces: Whether to start new traces if none exist. This will cause duplication if the underlying
                trace provider actually process start events. Primarily designed for use with Open Telemetry integration.
        """
        super().__init__()
        self._payload_converter = payload_converter
        self._start_traces = start_traces
        self._add_temporal_spans = add_temporal_spans

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """Intercepts client calls to propagate trace context.

        Args:
            next: The next interceptor in the chain.

        Returns:
            An interceptor that propagates trace context for client operations.
        """
        return _ContextPropagationClientOutboundInterceptor(
            next, self._add_temporal_spans
        )

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Intercepts activity calls to propagate trace context.

        Args:
            next: The next interceptor in the chain.

        Returns:
            An interceptor that propagates trace context for activity operations.
        """
        return _ContextPropagationActivityInboundInterceptor(
            next, self._add_temporal_spans
        )

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[_ContextPropagationWorkflowInboundInterceptor]:
        """Returns the workflow interceptor class to propagate trace context.

        Args:
            input: The input for creating the workflow interceptor.

        Returns:
            The class of the workflow interceptor that propagates trace context.
        """

        class ModifiedInterceptor(_ContextPropagationWorkflowInboundInterceptor):
            start_trace = self._start_traces
            add_temporal_spans = self._add_temporal_spans

        return ModifiedInterceptor


@contextmanager
def _maybe_span(add_temporal_spans: bool, span_name: str, data: dict[str, Any] | None):
    if add_temporal_spans and get_trace_provider().get_current_trace() is not None:
        with custom_span(name=span_name, data=data):
            yield
    else:
        yield


class _ContextPropagationClientOutboundInterceptor(
    temporalio.client.OutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._add_temporal_spans = add_temporal_spans

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        data = {"workflowId": input.id} if input.id else None
        span_name = "temporal:startWorkflow"
        with _maybe_span(
            self._add_temporal_spans,
            span_name + ":" + input.workflow,
            data=data,
        ):
            set_header_from_context(input)
            return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        data = {"workflowId": input.id, "query": input.query}
        span_name = "temporal:queryWorkflow"
        with _maybe_span(
            self._add_temporal_spans,
            span_name,
            data=data,
        ):
            set_header_from_context(input)
            return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        data = {"workflowId": input.id, "signal": input.signal}
        span_name = "temporal:signalWorkflow"
        with _maybe_span(
            self._add_temporal_spans,
            span_name,
            data=data,
        ):
            set_header_from_context(input)
            await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        data = {
            **({"workflowId": input.id} if input.id else {}),
            "update": input.update,
        }
        span_name = "temporal:updateWorkflow"
        with _maybe_span(
            self._add_temporal_spans,
            span_name,
            data=data,
        ):
            set_header_from_context(input)
            return await self.next.start_workflow_update(input)


class _ContextPropagationActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._add_temporal_spans = add_temporal_spans

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        with context_from_header(input, temporalio.activity.payload_converter()):
            with temporal_span(self._add_temporal_spans, "temporal:executeActivity"):
                return await self.next.execute_activity(input)


class _ContextPropagationWorkflowInboundInterceptor(
    temporalio.worker.WorkflowInboundInterceptor
):
    start_trace: bool = False
    add_temporal_spans: bool = True

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        class ModifiedInterceptor(_ContextPropagationWorkflowOutboundInterceptor):
            add_temporal_spans = self.add_temporal_spans

        self.next.init(ModifiedInterceptor(outbound))

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        _ensure_tracing_random()
        with context_from_header(
            input, temporalio.workflow.payload_converter(), start_trace=self.start_trace
        ):
            with temporal_span(self.add_temporal_spans, "temporal:executeWorkflow"):
                return await self.next.execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        _ensure_tracing_random()
        with context_from_header(input, temporalio.workflow.payload_converter()):
            with temporal_span(self.add_temporal_spans, "temporal:handleSignal"):
                return await self.next.handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        _ensure_tracing_random()
        # with context_from_header(input, temporalio.workflow.payload_converter()):
        with temporal_span(self.add_temporal_spans, "temporal:handleQuery"):
            return await self.next.handle_query(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        with context_from_header(
            input,
            temporalio.workflow.payload_converter(),
        ):
            self.next.handle_update_validator(input)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        _ensure_tracing_random()
        with context_from_header(
            input,
            temporalio.workflow.payload_converter(),
        ):
            return await self.next.handle_update_handler(input)


class _ContextPropagationWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    add_temporal_spans: bool = True

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        with _maybe_span(
            self.add_temporal_spans,
            "temporal:signalChildWorkflow",
            data={"workflowId": input.child_workflow_id},
        ):
            set_header_from_context(input)
            await self.next.signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with _maybe_span(
            self.add_temporal_spans,
            "temporal:signalExternalWorkflow",
            data={"workflowId": input.workflow_id},
        ):
            set_header_from_context(input)
            await self.next.signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        trace = get_trace_provider().get_current_trace()
        span: Span | None = None
        if trace and self.add_temporal_spans:
            span = custom_span(
                name="temporal:startActivity", data={"activity": input.activity}
            )
            span.start(mark_as_current=True)

        set_header_from_context(input)
        handle = self.next.start_activity(input)
        if span:
            handle.add_done_callback(lambda _: span.finish())  # type: ignore
        return handle

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        trace = get_trace_provider().get_current_trace()
        span: Span | None = None
        if trace and self.add_temporal_spans:
            span = custom_span(
                name="temporal:startChildWorkflow", data={"workflow": input.workflow}
            )
            span.start(mark_as_current=True)
        set_header_from_context(input)
        handle = await self.next.start_child_workflow(input)
        if span:
            handle.add_done_callback(lambda _: span.finish())  # type: ignore
        return handle

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        trace = get_trace_provider().get_current_trace()
        span: Span | None = None
        if trace and self.add_temporal_spans:
            span = custom_span(
                name="temporal:startLocalActivity", data={"activity": input.activity}
            )
            span.start(mark_as_current=True)
        set_header_from_context(input)
        handle = self.next.start_local_activity(input)
        if span:
            handle.add_done_callback(lambda _: span.finish())  # type: ignore
        return handle
