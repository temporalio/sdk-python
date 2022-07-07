"""OpenTelemetry interceptor that creates/propagates spans."""

from __future__ import annotations

import pickle
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
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
                :py:func:`opentelemetry.trace.get_tracer`. Currently, the tracer
                must be an instance of
                :py:class:`opentelemetry.sdk.trace.Tracer` due to internal
                serialization approaches used.
        """
        self.tracer = tracer or opentelemetry.trace.get_tracer(__name__)
        # Due to the fact that we have to violate the API to make spans
        # writeable after deserializing so it can cross the sandbox, we must
        # have an SDK tracer instance
        if not isinstance(self.tracer, opentelemetry.sdk.trace.Tracer):
            raise ValueError("Currently only SDK tracers supported")
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
                __temporal_opentelemetry_start_span=self._start_workflow_span,
                __temporal_opentelemetry_end_span=self._end_workflow_span,
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

    def _start_workflow_span(
        self,
        carrier: _CarrierDict,
        name: str,
        attrs: opentelemetry.util.types.Attributes,
    ) -> bytes:
        # Carrier to context, start span, set span as current on context,
        # context back to carrier
        context = self.text_map_propagator.extract(carrier)
        span = self.tracer.start_span(name, context, attributes=attrs)
        return _pickle_span(span)

    def _end_workflow_span(self, b: bytes) -> None:
        _unpickle_span(b, self.tracer).end()


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
    __temporal_opentelemetry_start_span: Callable[
        [_CarrierDict, str, opentelemetry.util.types.Attributes], bytes
    ]
    __temporal_opentelemetry_end_span: Callable[[bytes], None]


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
        with self._start_as_current_span(
            f"RunWorkflow:{temporalio.workflow.info().workflow_type}",
            parent_headers=input.headers,
            even_on_replay=True,
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_signal`.
        """
        with self._start_as_current_span(
            f"HandleSignal:{input.signal}",
            parent_headers=input.headers,
        ):
            await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_query`.
        """
        with self._start_as_current_span(
            f"HandleQuery:{input.query}",
            parent_headers=input.headers,
            even_on_replay=True,
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
    def _start_as_current_span(
        self,
        name: str,
        *,
        parent_headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]] = None,
        input: Optional[_InputWithMutableHeaders] = None,
        even_on_replay: bool = False,
    ) -> Iterator[None]:
        # If not even on replay and we are replaying, do nothing here
        if not even_on_replay and temporalio.workflow.unsafe.is_replaying():
            yield None
            return
        # Serialize the current or header context
        parent_carrier: _CarrierDict
        if parent_headers and self.header_key in parent_headers:
            parent_carrier = self.payload_converter.from_payloads(
                [parent_headers[self.header_key]]
            )[0]
        else:
            parent_carrier = {}
            self.text_map_propagator.inject(parent_carrier)

        # Always set span attributes as workflow ID and run ID
        info = temporalio.workflow.info()
        attrs = {"temporalWorkflowID": info.workflow_id, "temporalRunID": info.run_id}

        # Ask host to start span for us
        span_bytes = self._extern_functions["__temporal_opentelemetry_start_span"](
            parent_carrier, name, attrs
        )
        span = _unpickle_span(span_bytes)

        # We need to end the span on the caller once done. We have to disable
        # this call from ending the span, otherwise when it's serialized and
        # received upstream, attempts to end it there will fail as already done.
        #
        # We could just replace the fairly-simple use_span with our own here,
        # but it's easier to just leverage it for its internals like exception
        # setting.
        try:
            with opentelemetry.trace.use_span(span, end_on_exit=False):
                # If there is an input, set the headers as the current context
                if input:
                    carrier: _CarrierDict = {}
                    self.text_map_propagator.inject(carrier)
                    input.headers = self._context_carrier_to_headers(
                        carrier, input.headers
                    )
                yield None
        finally:
            # End the span on the host side
            self._extern_functions["__temporal_opentelemetry_end_span"](
                _pickle_span(span)
            )
            # Now we can end the span locally
            span.end()


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
        with self.root._start_as_current_span(
            f"SignalChildWorkflow:{input.signal}",
            input=input,
        ):
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with self.root._start_as_current_span(
            f"SignalExternalWorkflow:{input.signal}",
            input=input,
        ):
            return await super().signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._start_as_current_span(
            f"StartActivity:{input.activity}", input=input
        ):
            return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        with self.root._start_as_current_span(
            f"StartChildWorkflow:{input.workflow}",
            input=input,
        ):
            return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._start_as_current_span(
            f"StartActivity:{input.activity}",
            input=input,
        ):
            return super().start_local_activity(input)


# TODO(cretz): Is there any way to serialize/deserialize a normal writeable span
# using public library API?
@dataclass(frozen=True)
class _PicklableSpan:
    name: str
    context: opentelemetry.trace.SpanContext
    parent: Optional[opentelemetry.trace.SpanContext]
    attributes: opentelemetry.util.types.Attributes
    events: Sequence[opentelemetry.sdk.trace.Event]
    links: Sequence[opentelemetry.trace.Link]
    status: opentelemetry.sdk.trace.Status
    start_time: Optional[int]
    end_time: Optional[int]

    @staticmethod
    def from_span(span: opentelemetry.trace.Span) -> _PicklableSpan:
        # We extract from the SDK span, but don't use properties in some cases
        # to avoid unnecessary copies
        if not isinstance(span, opentelemetry.sdk.trace.ReadableSpan):
            raise TypeError(f"Expected span to be SDK span, was {type(span)}")
        # Attribute wrapper is not picklable
        attributes = span._attributes
        if attributes is not None:
            attributes = dict(attributes)
        return _PicklableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            attributes=attributes,
            events=span.events,
            links=span.links,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
        )

    def to_span(
        self, tracer: Optional[opentelemetry.trace.Tracer]
    ) -> opentelemetry.trace.Span:
        # Use simpler span instance if no tracer available. Cannot reuse this
        # code because some defaults are private so we have to avoid setting the
        # kwarg at all (and don't want to use a **dict).
        span: opentelemetry.sdk.trace._Span
        if not tracer:
            span = opentelemetry.sdk.trace._Span(
                name=self.name,
                context=self.context,
                parent=self.parent,
                attributes=self.attributes,
                events=self.events,
                links=self.links,
            )
        elif not isinstance(tracer, opentelemetry.sdk.trace.Tracer):
            # We currently only support the SDK tracer
            raise TypeError(f"Expected tracer to be SDK tracer, was {type(tracer)}")
        else:
            span = opentelemetry.sdk.trace._Span(
                name=self.name,
                context=self.context,
                parent=self.parent,
                attributes=self.attributes,
                events=self.events,
                links=self.links,
                # All of the tracer-specific pieces are below
                sampler=tracer.sampler,
                resource=tracer.resource,
                span_processor=tracer.span_processor,
                instrumentation_info=tracer.instrumentation_info,
                limits=tracer._span_limits,
                instrumentation_scope=tracer._instrumentation_scope,
            )
        span._status = self.status
        span._start_time = self.start_time
        span._end_time = self.end_time
        return span


def _pickle_span(span: opentelemetry.trace.Span) -> bytes:
    return pickle.dumps(_PicklableSpan.from_span(span))


def _unpickle_span(
    b: bytes, tracer: Optional[opentelemetry.trace.Tracer] = None
) -> opentelemetry.trace.Span:
    span: _PicklableSpan = pickle.loads(b)
    return span.to_span(tracer)
