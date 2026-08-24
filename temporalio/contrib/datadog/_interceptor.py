"""Datadog tracing interceptor for Temporal."""

from collections.abc import Callable, Mapping
from typing import Any

import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from temporalio.contrib.datadog._activity_interceptor import _ActivityInboundInterceptor
from temporalio.contrib.datadog._client_interceptor import _ClientOutboundInterceptor
from temporalio.contrib.datadog._constants import (
    CONTINUE_AS_NEW_TAG,
    DEFAULT_HEADER_KEY,
    OperationNames,
)
from temporalio.contrib.datadog._id_generator import gen_span_id, gen_trace_id
from temporalio.contrib.datadog._nexus_interceptor import (
    _NexusOperationInboundInterceptor,
)
from temporalio.contrib.datadog._propagator import _Propagator
from temporalio.contrib.datadog._span_annotator import _SpanAnnotator
from temporalio.contrib.datadog._workflow_interceptor import (
    DatadogTracingWorkflowInboundInterceptor,
    WorkflowTracingConfig,
    _active_workflow_span,
    _trace_disconnected,
)
from temporalio.contrib.datadog._wrapped_tracer import (
    FinishContext,
    FinishResult,
    WrappedTracer,
)


class DatadogTracingInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    def __init__(  # type: ignore[reportMissingSuperCall]
        self,
        tracer: Any | None = None,
        *,
        service_name: str | None = None,
        header_key: str = DEFAULT_HEADER_KEY,
        extra_tags: Mapping[str, str] | None = None,
        on_span_finish: Callable[[FinishContext], FinishResult | None] | None = None,
        workflow_tracing_config: WorkflowTracingConfig = WorkflowTracingConfig.default_config(),
        allow_invalid_parent_spans: bool = False,
    ) -> None:
        self.workflow_tracing_config = workflow_tracing_config

        self.propagator = _Propagator(
            header_key=header_key,
            service_name=service_name,
            payload_converter=temporalio.converter.PayloadConverter.default,
            allow_invalid_parent_spans=allow_invalid_parent_spans,
        )

        self.tracer = WrappedTracer(
            service_name=service_name,
            tracer=tracer,
            on_span_finish=on_span_finish,
            annotator=_SpanAnnotator(service_name=service_name, extra_tags=extra_tags),
            propagator=self.propagator,
        )

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _ClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _ActivityInboundInterceptor(next, self)

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        return _NexusOperationInboundInterceptor(next, self)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[DatadogTracingWorkflowInboundInterceptor]:
        input.unsafe_extern_functions["__temporal_datadog_start_sandboxed_span"] = (
            self._start_sandboxed_span
        )
        input.unsafe_extern_functions["__temporal_datadog_finish_sandboxed_span"] = (
            self._finish_sandboxed_span
        )
        input.unsafe_extern_functions[
            "__temporal_datadog_configure_workflow_tracing"
        ] = self._configure_workflow_tracing
        # NOTE: fork-specific workaround; reconsider if this integration is ever proposed upstream.
        # These two externs let workflow code cross the sandbox boundary to read/write
        # ContextVars that live in the host module.  Without them, the sandbox's own
        # copy of workflow_interceptor has fresh ContextVars that are never set.
        input.unsafe_extern_functions["__temporal_datadog_get_workflow_span"] = (
            _active_workflow_span.get
        )
        input.unsafe_extern_functions["__temporal_datadog_set_trace_disconnected"] = (
            lambda: _trace_disconnected.set(True)
        )
        return DatadogTracingWorkflowInboundInterceptor

    def _configure_workflow_tracing(self):
        return self.propagator, self.workflow_tracing_config

    def _start_sandboxed_span(
        self,
        operation_name: str,
        resource_name: str,
        attributes: dict[str, Any] | None,
        parent_ctx: Any | None,
        idempotency_key: str | None,
        start_time: int | None = None,
    ) -> Any:
        # No DD header (uninstrumented client): pass a deterministic trace_id to keep
        # the RunWorkflow trace consistent if the worker restarts mid-run.
        det_trace_id = (
            gen_trace_id(idempotency_key)
            if operation_name == OperationNames.RUN_WORKFLOW
            and parent_ctx is None
            and idempotency_key is not None
            else None
        )
        span = self.tracer.start_span(
            operation_name=operation_name,
            parent_ctx=parent_ctx,
            resource_name=resource_name,
            activate=False,
            start_time=start_time,
            span_id=gen_span_id(idempotency_key)
            if idempotency_key is not None
            else None,
            attributes=attributes,
            parent_from_header=True,
            trace_id=det_trace_id,
        )
        # NOTE: fork-specific workaround; reconsider if this integration is ever proposed upstream.
        # Expose the RunWorkflow span to the host-side ContextVar so that
        # span_from_workflow_context() can return it via the extern.
        if operation_name == OperationNames.RUN_WORKFLOW:
            _active_workflow_span.set(span)
        return span

    def _finish_sandboxed_span(
        self,
        operation_name: str,
        span: Any | None,
        operation_exc: BaseException | None,
    ) -> None:
        if span is None:
            return

        if isinstance(operation_exc, temporalio.workflow.ContinueAsNewError):
            span.set_tag(CONTINUE_AS_NEW_TAG, True)

        self.tracer.finish_span(span, operation_name, operation_exc)
