"""OpenTelemetry interceptor that creates/propagates spans."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import (
    Any,
    NoReturn,
    TypeAlias,
)

import nexusrpc.handler
import opentelemetry.baggage.propagation
import opentelemetry.context
import opentelemetry.propagators.composite
import opentelemetry.propagators.textmap
import opentelemetry.trace
import opentelemetry.trace.propagation.tracecontext
import opentelemetry.util.types
from opentelemetry.context import Context
from opentelemetry.trace import (
    Status,
    StatusCode,
    Tracer,
)
from typing_extensions import Protocol

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.contrib.opentelemetryv2.workflow
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from temporalio import workflow
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory

# OpenTelemetry dynamically, lazily chooses its context implementation at
# runtime. When first accessed, they use pkg_resources.iter_entry_points + load.
# The load uses built-in open() which we don't allow in sandbox mode at runtime,
# only import time. Therefore if the first use of a OTel context is inside the
# sandbox, which it may be for a workflow worker, this will fail. So instead we
# eagerly reference it here to force loading at import time instead of lazily.
opentelemetry.context.get_current()

default_text_map_propagator = opentelemetry.propagators.composite.CompositePropagator(
    [
        opentelemetry.trace.propagation.tracecontext.TraceContextTextMapPropagator(),
        opentelemetry.baggage.propagation.W3CBaggagePropagator(),
    ]
)
"""Default text map propagator used by :py:class:`TracingInterceptor`."""

_CarrierDict: TypeAlias = dict[str, opentelemetry.propagators.textmap.CarrierValT]


def _context_to_headers(
    headers: Mapping[str, temporalio.api.common.v1.Payload],
) -> Mapping[str, temporalio.api.common.v1.Payload]:
    carrier: _CarrierDict = {}
    default_text_map_propagator.inject(carrier)
    if carrier:
        headers = {
            **headers,
            "_tracer-data": temporalio.converter.PayloadConverter.default.to_payloads(
                [carrier]
            )[0],
        }
    return headers


def _context_to_nexus_headers(headers: Mapping[str, str]) -> Mapping[str, str]:
    carrier: _CarrierDict = {}
    default_text_map_propagator.inject(carrier)
    if carrier:
        out = {**headers} if headers else {}
        for k, v in carrier.items():
            if isinstance(v, list):
                out[k] = ",".join(v)
            else:
                out[k] = v
        return out
    else:
        return headers


def _headers_to_context(
    headers: Mapping[str, temporalio.api.common.v1.Payload],
) -> Context:
    context_header = headers.get("_tracer-data")
    if context_header:
        context_carrier: _CarrierDict = (
            temporalio.converter.PayloadConverter.default.from_payloads(
                [context_header]
            )[0]
        )

        context = default_text_map_propagator.extract(context_carrier)
    else:
        context = opentelemetry.context.Context()
    return context


def _nexus_headers_to_context(headers: Mapping[str, str]) -> Context:
    context = default_text_map_propagator.extract(headers)
    return context


def _ensure_tracer(tracer: Tracer) -> None:
    """We use a custom uuid generator for spans to ensure that changes to user code workflow.random usage
    do not affect tracing and vice versa.
    """
    instance = workflow.instance()
    if not hasattr(instance, "__temporal_opentelemetry_tracer"):
        setattr(
            workflow.instance(),
            "__temporal_opentelemetry_tracer",
            tracer,
        )


@contextmanager
def _maybe_span(
    tracer: Tracer,
    name: str,
    *,
    add_temporal_spans: bool,
    attributes: opentelemetry.util.types.Attributes,
    kind: opentelemetry.trace.SpanKind,
    context: Context | None = None,
) -> Iterator[None]:
    if not add_temporal_spans:
        yield
    else:
        token = opentelemetry.context.attach(context) if context else None
        try:
            span_factory = (
                temporalio.contrib.opentelemetryv2.workflow.start_as_current_span
                if workflow.in_workflow()
                else tracer.start_as_current_span
            )
            with span_factory(
                name,
                attributes=attributes,
                kind=kind,
                context=context,
                set_status_on_exception=False,
            ) as span:
                try:
                    yield
                except Exception as exc:
                    if (
                        not isinstance(exc, ApplicationError)
                        or exc.category != ApplicationErrorCategory.BENIGN
                    ):
                        span.set_status(
                            Status(
                                status_code=StatusCode.ERROR,
                                description=f"{type(exc).__name__}: {exc}",
                            )
                        )
                    raise
        finally:
            if token and context is opentelemetry.context.get_current():
                opentelemetry.context.detach(token)


class TracingInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    """Interceptor that supports client and worker OpenTelemetry span creation
    and propagation.

    This should be created and used for ``interceptors`` on the
    :py:meth:`temporalio.client.Client.connect` call to apply to all client
    calls and worker calls using that client. To only apply to workers, set as
    worker creation option instead of in client.

    To customize the header key, text map propagator, or payload converter, a
    subclass of this and :py:class:`TracingWorkflowInboundInterceptor` should be
    created. In addition to customizing those attributes, the subclass of this
    class should return the workflow interceptor subclass from
    :py:meth:`workflow_interceptor_class`. That subclass should also set the
    custom attributes desired.
    """

    def __init__(  # type: ignore[reportMissingSuperCall]
        self,
        tracer: opentelemetry.trace.Tracer,
        add_temporal_spans: bool = False,
    ) -> None:
        """Initialize a OpenTelemetry tracing interceptor."""
        self._tracer = tracer
        self._add_temporal_spans = add_temporal_spans

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.client.Interceptor.intercept_client`.
        """
        return _TracingClientOutboundInterceptor(
            next, self._tracer, self._add_temporal_spans
        )

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.intercept_activity`.
        """
        return _TracingActivityInboundInterceptor(
            next, self._tracer, self._add_temporal_spans
        )

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[_TracingWorkflowInboundInterceptor]:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.workflow_interceptor_class`.
        """
        tracer = self._tracer

        class InterceptorWithState(_TracingWorkflowInboundInterceptor):
            _add_temporal_spans = self._add_temporal_spans

            def get_tracer(self):
                return tracer

        return InterceptorWithState

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.intercept_nexus_operation`.
        """
        return _TracingNexusOperationInboundInterceptor(
            next, self._tracer, self._add_temporal_spans
        )


class _TracingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        tracer: Tracer,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._add_temporal_spans = add_temporal_spans
        self._tracer = tracer

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        prefix = (
            "StartWorkflow" if not input.start_signal else "SignalWithStartWorkflow"
        )
        with _maybe_span(
            self._tracer,
            f"{prefix}:{input.workflow}",
            add_temporal_spans=self._add_temporal_spans,
            attributes={"temporalWorkflowID": input.id},
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        with _maybe_span(
            self._tracer,
            f"QueryWorkflow:{input.query}",
            add_temporal_spans=self._add_temporal_spans,
            attributes={"temporalWorkflowID": input.id},
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        with _maybe_span(
            self._tracer,
            f"SignalWorkflow:{input.signal}",
            add_temporal_spans=self._add_temporal_spans,
            attributes={"temporalWorkflowID": input.id},
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        with _maybe_span(
            self._tracer,
            f"StartWorkflowUpdate:{input.update}",
            add_temporal_spans=self._add_temporal_spans,
            attributes={"temporalWorkflowID": input.id},
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        attrs = {
            "temporalWorkflowID": input.start_workflow_input.id,
        }
        if input.update_workflow_input.update_id is not None:
            attrs["temporalUpdateID"] = input.update_workflow_input.update_id

        with _maybe_span(
            self._tracer,
            f"StartUpdateWithStartWorkflow:{input.start_workflow_input.workflow}",
            add_temporal_spans=self._add_temporal_spans,
            attributes=attrs,
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.start_workflow_input.headers = _context_to_headers(
                input.start_workflow_input.headers
            )
            input.update_workflow_input.headers = _context_to_headers(
                input.update_workflow_input.headers
            )
            return await super().start_update_with_start_workflow(input)


class _TracingActivityInboundInterceptor(temporalio.worker.ActivityInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        tracer: Tracer,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._tracer = tracer
        self._add_temporal_spans = add_temporal_spans

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        context = _headers_to_context(input.headers)
        token = opentelemetry.context.attach(context)
        try:
            info = temporalio.activity.info()
            with _maybe_span(
                self._tracer,
                f"RunActivity:{info.activity_type}",
                add_temporal_spans=self._add_temporal_spans,
                attributes={
                    "temporalWorkflowID": info.workflow_id,
                    "temporalRunID": info.workflow_run_id,
                    "temporalActivityID": info.activity_id,
                },
                kind=opentelemetry.trace.SpanKind.SERVER,
            ):
                return await super().execute_activity(input)
        finally:
            if context is opentelemetry.context.get_current():
                opentelemetry.context.detach(token)


class _TracingNexusOperationInboundInterceptor(
    temporalio.worker.NexusOperationInboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.NexusOperationInboundInterceptor,
        tracer: Tracer,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._tracer = tracer
        self._add_temporal_spans = add_temporal_spans

    @contextmanager
    def _top_level_context(self, headers: Mapping[str, str]) -> Iterator[None]:
        context = _nexus_headers_to_context(headers)
        token = opentelemetry.context.attach(context)
        try:
            yield
        finally:
            if context is opentelemetry.context.get_current():
                opentelemetry.context.detach(token)

    async def execute_nexus_operation_start(
        self, input: temporalio.worker.ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any]
        | nexusrpc.handler.StartOperationResultAsync
    ):
        with self._top_level_context(input.ctx.headers):
            with _maybe_span(
                self._tracer,
                f"RunStartNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                add_temporal_spans=self._add_temporal_spans,
                attributes={},
                kind=opentelemetry.trace.SpanKind.SERVER,
            ):
                return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        with self._top_level_context(input.ctx.headers):
            with _maybe_span(
                self._tracer,
                f"RunCancelNexusOperationHandler:{input.ctx.service}/{input.ctx.operation}",
                add_temporal_spans=self._add_temporal_spans,
                attributes={},
                kind=opentelemetry.trace.SpanKind.SERVER,
            ):
                return await self.next.execute_nexus_operation_cancel(input)


class _InputWithHeaders(Protocol):
    headers: Mapping[str, temporalio.api.common.v1.Payload]


class _TracingWorkflowInboundInterceptor(temporalio.worker.WorkflowInboundInterceptor):
    """Tracing interceptor for workflow calls."""

    _add_temporal_spans: bool = False

    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        """Initialize a tracing workflow interceptor."""
        super().__init__(next)

    def get_tracer(self) -> Tracer:
        raise NotImplementedError()

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.init`.
        """
        super().init(
            _TracingWorkflowOutboundInterceptor(
                outbound, self.get_tracer(), self._add_temporal_spans
            )
        )

    @contextmanager
    def _workflow_maybe_span(self, name: str) -> Iterator[None]:
        info = temporalio.workflow.info()
        attributes: dict[str, opentelemetry.util.types.AttributeValue] = {
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        with _maybe_span(
            self.get_tracer(),
            name,
            add_temporal_spans=self._add_temporal_spans,
            attributes=attributes,
            kind=opentelemetry.trace.SpanKind.SERVER,
        ):
            yield

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.execute_workflow`.
        """
        _ensure_tracer(self.get_tracer())
        with self._top_level_workflow_context(input):
            with self._workflow_maybe_span(
                f"RunWorkflow:{temporalio.workflow.info().workflow_type}"
            ):
                return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_signal`.
        """
        _ensure_tracer(self.get_tracer())
        with self._top_level_workflow_context(input):
            with self._workflow_maybe_span(
                f"HandleSignal:{input.signal}",
            ):
                await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_query`.
        """
        _ensure_tracer(self.get_tracer())
        with self._top_level_workflow_context(input):
            with self._workflow_maybe_span(
                f"HandleQuery:{input.query}",
            ):
                return await super().handle_query(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_update_validator`.
        """
        _ensure_tracer(self.get_tracer())
        with self._top_level_workflow_context(input):
            with self._workflow_maybe_span(
                f"ValidateUpdate:{input.update}",
            ):
                super().handle_update_validator(input)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_update_handler`.
        """
        _ensure_tracer(self.get_tracer())
        with self._top_level_workflow_context(input):
            with self._workflow_maybe_span(
                f"HandleUpdate:{input.update}",
            ):
                return await super().handle_update_handler(input)

    @contextmanager
    def _top_level_workflow_context(self, input: _InputWithHeaders) -> Iterator[None]:
        context = _headers_to_context(input.headers)
        token = opentelemetry.context.attach(context)
        try:
            yield
        finally:
            if context is opentelemetry.context.get_current():
                opentelemetry.context.detach(token)


class _TracingWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        tracer: Tracer,
        add_temporal_spans: bool,
    ) -> None:
        super().__init__(next)
        self._tracer = tracer
        self._add_temporal_spans = add_temporal_spans

    @contextmanager
    def _workflow_maybe_span(
        self, name: str, kind: opentelemetry.trace.SpanKind
    ) -> Iterator[None]:
        info = temporalio.workflow.info()
        attributes: dict[str, opentelemetry.util.types.AttributeValue] = {
            "temporalWorkflowID": info.workflow_id,
            "temporalRunID": info.run_id,
        }
        with _maybe_span(
            self._tracer,
            name,
            add_temporal_spans=self._add_temporal_spans,
            attributes=attributes,
            kind=kind,
        ):
            yield

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        input.headers = _context_to_headers(input.headers)
        super().continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        with self._workflow_maybe_span(
            f"SignalChildWorkflow:{input.signal}",
            kind=opentelemetry.trace.SpanKind.SERVER,
        ):
            input.headers = _context_to_headers(input.headers)
            await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with self._workflow_maybe_span(
            f"SignalExternalWorkflow:{input.signal}",
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            await super().signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self._workflow_maybe_span(
            f"StartActivity:{input.activity}",
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        with self._workflow_maybe_span(
            f"StartChildWorkflow:{input.workflow}",
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self._workflow_maybe_span(
            f"StartActivity:{input.activity}",
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_headers(input.headers)
            return super().start_local_activity(input)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        with self._workflow_maybe_span(
            f"StartNexusOperation:{input.service}/{input.operation_name}",
            kind=opentelemetry.trace.SpanKind.CLIENT,
        ):
            input.headers = _context_to_nexus_headers(input.headers or {})
            return await super().start_nexus_operation(input)
