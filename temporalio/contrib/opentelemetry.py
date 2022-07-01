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
    Tuple,
    Type,
    cast,
)

import opentelemetry.baggage.propagation
import opentelemetry.context
import opentelemetry.context.context
import opentelemetry.propagators.composite
import opentelemetry.propagators.textmap
import opentelemetry.trace
import opentelemetry.trace.propagation.tracecontext
import opentelemetry.util.types
from typing_extensions import TypeAlias, TypedDict

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

_CarrierDict: TypeAlias = Dict[str, opentelemetry.propagators.textmap.CarrierValT]


class TracingInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    def __init__(self, tracer: Optional[opentelemetry.trace.Tracer] = None) -> None:
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
        return _TracingClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _TracingActivityInboundInterceptor(next, self)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> Type[TracingWorkflowInboundInterceptor]:
        # Set the externs needed
        # TODO(cretz): MyPy works w/ spread kwargs instead of direct passing
        input.extern_functions.update(
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

    def _start_workflow_span(
        self,
        carrier: _CarrierDict,
        name: str,
        attrs: opentelemetry.util.types.Attributes,
    ) -> _CarrierDict:
        # Carrier to context, start span, set span as current on context,
        # context back to carrier
        context = self.text_map_propagator.extract(carrier)
        span = self.tracer.start_span(name, context, attributes=attrs)
        context = opentelemetry.trace.set_span_in_context(span, context)
        carrier = {}
        self.text_map_propagator.inject(carrier, context)
        return carrier

    def _end_workflow_span(self, carrier: _CarrierDict) -> None:
        # Carrier to context, span from context, end span
        context = self.text_map_propagator.extract(carrier)
        span = opentelemetry.trace.get_current_span(context)
        span.end()


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
        with self.root.tracer.start_as_current_span(
            f"{prefix}:{input.workflow}", attributes={"temporalWorkflowID": input.id}
        ):
            input.headers = self.root._context_to_headers(input.headers)
            return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        with self.root.tracer.start_as_current_span(
            f"QueryWorkflow:{input.query}", attributes={"temporalWorkflowID": input.id}
        ):
            input.headers = self.root._context_to_headers(input.headers)
            return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        with self.root.tracer.start_as_current_span(
            f"SignalWorkflow:{input.signal}",
            attributes={"temporalWorkflowID": input.id},
        ):
            input.headers = self.root._context_to_headers(input.headers)
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
        [_CarrierDict, str, opentelemetry.util.types.Attributes], _CarrierDict
    ]
    __temporal_opentelemetry_end_span: Callable[[_CarrierDict], None]


class TracingWorkflowInboundInterceptor(temporalio.worker.WorkflowInboundInterceptor):
    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
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
        super().init(_TracingWorkflowOutboundInterceptor(outbound, self))

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        with self._start_as_current_span(
            f"RunWorkflow:{temporalio.workflow.info().workflow_type}",
            parent_headers=input.headers,
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with self._start_as_current_span(
            f"HandleSignal:{input.signal}",
            parent_headers=input.headers,
        ):
            await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        with self._start_as_current_span(
            f"HandleQuery:{input.query}",
            parent_headers=input.headers,
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
    ) -> Iterator[_CarrierDict]:
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
        carrier = self._extern_functions["__temporal_opentelemetry_start_span"](
            parent_carrier, name, attrs
        )
        span = opentelemetry.trace.get_current_span(
            self.text_map_propagator.extract(carrier)
        )

        # We need to end the span on the caller once done
        with opentelemetry.trace.use_span(span):
            try:
                # Yield our already-serialized carrier so it can be used by
                # callers
                yield carrier
            finally:
                # End the span that is still serialized on the carrier
                self._extern_functions["__temporal_opentelemetry_end_span"](carrier)


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
            f"SignalChildWorkflow:{input.signal}"
        ) as carrier:
            input.headers = self.root._context_carrier_to_headers(
                carrier, input.headers
            )
            return await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with self.root._start_as_current_span(
            f"SignalExternalWorkflow:{input.signal}"
        ) as carrier:
            input.headers = self.root._context_carrier_to_headers(
                carrier, input.headers
            )
            return await super().signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._start_as_current_span(
            f"StartActivity:{input.activity}"
        ) as carrier:
            input.headers = self.root._context_carrier_to_headers(
                carrier, input.headers
            )
            return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        with self.root._start_as_current_span(
            f"StartChildWorkflow:{input.workflow}"
        ) as carrier:
            input.headers = self.root._context_carrier_to_headers(
                carrier, input.headers
            )
            return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._start_as_current_span(
            f"StartActivity:{input.activity}"
        ) as carrier:
            input.headers = self.root._context_carrier_to_headers(
                carrier, input.headers
            )
            return super().start_local_activity(input)
