"""OpenTelemetry interceptor that creates/propagates spans."""

from __future__ import annotations

from contextlib import contextmanager
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    Mapping,
    NoReturn,
    Optional,
    Type,
    cast,
)

import opentelemetry.baggage.propagation
import opentelemetry.context
import opentelemetry.context.context
import opentelemetry.propagators.composite
import opentelemetry.propagators.textmap
import opentelemetry.sdk.trace
import opentelemetry.sdk.util.instrumentation
import opentelemetry.trace
import opentelemetry.trace.propagation.tracecontext
import opentelemetry.util.types
from typing_extensions import Protocol, TypeAlias, TypedDict

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow

default_text_map_propagator = opentelemetry.propagators.composite.CompositePropagator(
    [
        opentelemetry.trace.propagation.tracecontext.TraceContextTextMapPropagator(),
        opentelemetry.baggage.propagation.W3CBaggagePropagator(),
    ]
)
"""Default text map propagator used by :py:class:`TracingInterceptor`."""

_CarrierDict: TypeAlias = Dict[str, opentelemetry.propagators.textmap.CarrierValT]


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

    def __init__(
        self,
        tracer: Optional[opentelemetry.trace.Tracer] = None,
    ) -> None:
        """Initialize a OpenTelemetry tracing interceptor.

        Args:
            tracer: The tracer to use. Defaults to
                :py:func:`opentelemetry.trace.get_tracer`.
        """
        self.tracer = tracer or opentelemetry.trace.get_tracer(__name__)
        # To customize any of this, users must subclass. We intentionally don't
        # accept this in the constructor because if they're customizing these
        # values, they'd also need to do it on the workflow side via subclassing
        # on that interceptor since they can't accept custom constructor values.
        self.header_key: str = "_tracer-data"
        self.text_map_propagator: opentelemetry.propagators.textmap.TextMapPropagator = (
            default_text_map_propagator
        )
        # TODO(cretz): Should I be using the configured one at the client and activity level?
        self.payload_converter = temporalio.converter.default().payload_converter

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.client.Interceptor.intercept_client`.
        """
        return _TracingClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.intercept_activity`.
        """
        return _TracingActivityInboundInterceptor(next, self)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> Type[TracingWorkflowInboundInterceptor]:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.workflow_interceptor_class`.
        """
        # Set the externs needed
        # TODO(cretz): MyPy works w/ spread kwargs instead of direct passing
        input.unsafe_extern_functions.update(
            **_WorkflowExternFunctions(
                __temporal_opentelemetry_new_completed_span=self._new_completed_workflow_span,
            )
        )
        return TracingWorkflowInboundInterceptor

    def _context_to_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[Mapping[str, temporalio.api.common.v1.Payload]]:
        carrier: _CarrierDict = {}
        self.text_map_propagator.inject(carrier)
        if not carrier:
            return None
        new_headers = dict(headers) if headers is not None else {}
        new_headers[self.header_key] = self.payload_converter.to_payloads([carrier])[0]
        return new_headers

    def _context_from_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[opentelemetry.context.context.Context]:
        if not headers or self.header_key not in headers:
            return None
        header_payload = headers.get(self.header_key)
        if not header_payload:
            return None
        carrier: _CarrierDict = self.payload_converter.from_payloads([header_payload])[
            0
        ]
        if not carrier:
            return None
        return self.text_map_propagator.extract(carrier)

    @contextmanager
    def _start_as_current_span(
        self,
        name: str,
        *,
        attributes: opentelemetry.util.types.Attributes,
        input: Optional[_InputWithMutableHeaders] = None,
    ) -> Iterator[None]:
        with self.tracer.start_as_current_span(name, attributes=attributes):
            if input:
                input.headers = self._context_to_headers(input.headers)
            yield None

    def _new_completed_workflow_span(
        self,
        carrier: _CarrierDict,
        name: str,
        attrs: opentelemetry.util.types.Attributes,
        start_time_ns: int,
    ) -> _CarrierDict:
        # Carrier to context, start span, set span as current on context,
        # context back to carrier
        context = self.text_map_propagator.extract(carrier)
        # We start and end the span immediately because it is not replay-safe to
        # keep an unended long-running span. We set the end time the same as the
        # start time to make it clear it has no duration.
        span = self.tracer.start_span(
            name, context, attributes=attrs, start_time=start_time_ns
        )
        context = opentelemetry.trace.set_span_in_context(span, context)
        span.end(end_time=start_time_ns)
        # Back to carrier
        carrier = {}
        self.text_map_propagator.inject(carrier, context)
        return carrier


class _InputWithMutableHeaders(Protocol):
    headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]


class _TracingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self, next: temporalio.client.OutboundInterceptor, root: TracingInterceptor
    ) -> None:
        super().__init__(next)
        self.root = root

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        prefix = (
            "StartWorkflow" if not input.start_signal else "SignalWithStartWorkflow"
        )
        with self.root._start_as_current_span(
            f"{prefix}:{input.workflow}",
            attributes={"temporalWorkflowID": input.id},
            input=input,
        ):
            return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        with self.root._start_as_current_span(
            f"QueryWorkflow:{input.query}",
            attributes={"temporalWorkflowID": input.id},
            input=input,
        ):
            return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        with self.root._start_as_current_span(
            f"SignalWorkflow:{input.signal}",
            attributes={"temporalWorkflowID": input.id},
            input=input,
        ):
            return await super().signal_workflow(input)


class _TracingActivityInboundInterceptor(temporalio.worker.ActivityInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        root: TracingInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        info = temporalio.activity.info()
        with self.root.tracer.start_as_current_span(
            f"RunActivity:{info.activity_type}",
            context=self.root._context_from_headers(input.headers),
            attributes={
                "temporalWorkflowID": info.workflow_id,
                "temporalRunID": info.workflow_run_id,
                "temporalActivityID": info.activity_id,
            },
        ):
            return await super().execute_activity(input)


class _WorkflowExternFunctions(TypedDict):
    __temporal_opentelemetry_new_completed_span: Callable[
        [_CarrierDict, str, opentelemetry.util.types.Attributes, int], _CarrierDict
    ]


class TracingWorkflowInboundInterceptor(temporalio.worker.WorkflowInboundInterceptor):
    """Tracing interceptor for workflow calls.

    See :py:class:`TracingInterceptor` docs on why one might want to subclass
    this class.
    """

    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        """Initialize a tracing workflow interceptor."""
        super().__init__(next)
        self._extern_functions = cast(
            _WorkflowExternFunctions, temporalio.workflow.extern_functions()
        )
        # To customize these, like the primary tracing interceptor, subclassing
        # must be used
        self.header_key: str = "_tracer-data"
        self.text_map_propagator: opentelemetry.propagators.textmap.TextMapPropagator = (
            default_text_map_propagator
        )
        # TODO(cretz): Should I be using the configured one for this workflow?
        self.payload_converter = temporalio.converter.default().payload_converter

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.init`.
        """
        super().init(_TracingWorkflowOutboundInterceptor(outbound, self))

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.execute_workflow`.
        """
        with self._instrument(
            f"RunWorkflow:{temporalio.workflow.info().workflow_type}",
            parent_headers=input.headers,
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_signal`.
        """
        with self._instrument(
            f"HandleSignal:{input.signal}",
            parent_headers=input.headers,
        ):
            await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_query`.
        """
        with self._instrument(
            f"HandleQuery:{input.query}",
            parent_headers=input.headers,
            new_span_even_on_replay=True,
        ):
            return await super().handle_query(input)

    def _context_to_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[Mapping[str, temporalio.api.common.v1.Payload]]:
        carrier: _CarrierDict = {}
        self.text_map_propagator.inject(carrier)
        return self._context_carrier_to_headers(carrier, headers)

    def _context_carrier_to_headers(
        self,
        carrier: _CarrierDict,
        headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]],
    ) -> Optional[Mapping[str, temporalio.api.common.v1.Payload]]:
        if not carrier:
            return None
        new_headers = dict(headers) if headers is not None else {}
        new_headers[self.header_key] = self.payload_converter.to_payloads([carrier])[0]
        return new_headers

    @contextmanager
    def _instrument(
        self,
        span_name: str,
        *,
        parent_headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]] = None,
        input: Optional[_InputWithMutableHeaders] = None,
        new_span_even_on_replay: bool = False,
    ) -> Iterator[None]:
        context = opentelemetry.context.get_current()

        # If there are parent headers, we need to extract it
        if parent_headers and self.header_key in parent_headers:
            carrier = self.payload_converter.from_payloads(
                [parent_headers[self.header_key]]
            )[0]
            context = self.text_map_propagator.extract(carrier, context)

        # If a span is warranted, create via extern, and replace context with
        # result
        if new_span_even_on_replay or not temporalio.workflow.unsafe.is_replaying():
            # First serialize current context to carrier
            carrier = {}
            self.text_map_propagator.inject(carrier, context)
            # Always set span attributes as workflow ID and run ID
            info = temporalio.workflow.info()
            attrs = {
                "temporalWorkflowID": info.workflow_id,
                "temporalRunID": info.run_id,
            }
            # Invoke extern and recreate context from it
            carrier = self._extern_functions[
                "__temporal_opentelemetry_new_completed_span"
            ](carrier, span_name, attrs, temporalio.workflow.time_ns())
            context = self.text_map_propagator.extract(carrier, context)

        # If there is input, put the context as the header
        if input:
            carrier = {}
            self.text_map_propagator.inject(carrier, context)
            input.headers = self._context_carrier_to_headers(carrier, input.headers)

        # Attach the context, yield, then detach
        token = opentelemetry.context.attach(context)
        try:
            yield None
        finally:
            opentelemetry.context.detach(token)


class _TracingWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        root: TracingWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        input.headers = self.root._context_to_headers(input.headers)
        super().continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        with self.root._instrument(
            f"SignalChildWorkflow:{input.signal}",
            input=input,
        ):
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with self.root._instrument(
            f"SignalExternalWorkflow:{input.signal}",
            input=input,
        ):
            return await super().signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._instrument(f"StartActivity:{input.activity}", input=input):
            return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        with self.root._instrument(
            f"StartChildWorkflow:{input.workflow}",
            input=input,
        ):
            return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._instrument(
            f"StartActivity:{input.activity}",
            input=input,
        ):
            return super().start_local_activity(input)
