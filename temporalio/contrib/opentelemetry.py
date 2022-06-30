from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, NoReturn, Optional, Type

import opentelemetry.baggage.propagation
import opentelemetry.context.context
import opentelemetry.propagators.composite
import opentelemetry.propagators.textmap
import opentelemetry.trace
import opentelemetry.trace.propagation.tracecontext
import opentelemetry.util.types

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


class TracingInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    def __init__(
        self,
        tracer: Optional[opentelemetry.trace.Tracer] = None,
        header_key: str = "_tracer-data",
        text_map_propagator: opentelemetry.propagators.textmap.TextMapPropagator = default_text_map_propagator,
    ) -> None:
        self.tracer = tracer or opentelemetry.trace.get_tracer(__name__)
        self.header_key = header_key
        self.text_map_propagator = text_map_propagator
        # TODO(cretz): Allow customization?
        self._payload_converter = temporalio.converter.default().payload_converter

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
    ) -> Optional[Type[temporalio.worker.WorkflowInboundInterceptor]]:
        # TODO(cretz): Externs
        return super().workflow_interceptor_class(input)

    def _context_to_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[Mapping[str, temporalio.api.common.v1.Payload]]:
        # Serialize to dict
        carrier: Dict[str, opentelemetry.propagators.textmap.CarrierValT] = {}
        self.text_map_propagator.inject(carrier)
        if not carrier:
            return None
        new_headers = dict(headers) if headers is not None else {}
        new_headers[self.header_key] = self._payload_converter.to_payloads([carrier])[0]
        return new_headers

    def _context_from_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[opentelemetry.context.context.Context]:
        if not headers or self.header_key not in headers:
            return None
        header_payload = headers.get(self.header_key)
        if not header_payload:
            return None
        carrier: Dict[
            str, opentelemetry.propagators.textmap.CarrierValT
        ] = self._payload_converter.from_payloads([header_payload])[0]
        if not isinstance(carrier, dict):
            raise TypeError(
                f"Expected trace header to be dict type, got {type(carrier)}"
            )
        return self.text_map_propagator.extract(carrier)


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


class _TracingWorkflowInboundInterceptor(temporalio.worker.WorkflowInboundInterceptor):
    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        super().__init__(next)

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        return super().init(outbound)

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        with self._use_span(
            self._start_span(
                f"RunWorkflow:{temporalio.workflow.info().workflow_type}",
                parent_headers=input.headers,
            )
        ):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with self._use_span(
            self._start_span(
                f"HandleSignal:{input.signal}",
                parent_headers=input.headers,
            )
        ):
            await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        with self._use_span(
            self._start_span(
                f"HandleQuery:{input.query}",
                parent_headers=input.headers,
            )
        ):
            return await super().handle_query(input)

    def _context_to_headers(
        self, headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]]
    ) -> Optional[Mapping[str, temporalio.api.common.v1.Payload]]:
        pass

    def _start_span(
        self,
        name: str,
        *,
        parent_headers: Optional[Mapping[str, temporalio.api.common.v1.Payload]] = None,
    ) -> Optional[opentelemetry.trace.Span]:
        # TODO: attributes={"temporalWorkflowID": info.workflow_id, "temporalRunID": info.run_id},
        pass

    @contextmanager
    def _use_span(
        self,
        span: Optional[opentelemetry.trace.Span],
    ) -> Iterator[None]:
        raise NotImplementedError


class _TracingWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        root: _TracingWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        input.headers = self.root._context_to_headers(input.headers)
        super().continue_as_new(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._use_span(self.root._start_span(f"StartActivity")):
            input.headers = self.root._context_to_headers(input.headers)
            return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        with self.root._use_span(self.root._start_span(f"StartChildWorkflow")):
            input.headers = self.root._context_to_headers(input.headers)
            return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with self.root._use_span(self.root._start_span(f"StartActivity")):
            input.headers = self.root._context_to_headers(input.headers)
            return super().start_local_activity(input)
