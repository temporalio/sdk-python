"""OpenTelemetry interceptor that creates/propagates spans."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Generic,
    NoReturn,
    TypeAlias,
    TypeVar,
    cast,
)

import nexusrpc.handler
import opentelemetry.baggage.propagation
import opentelemetry.context
import opentelemetry.context.context
import opentelemetry.propagators.composite
import opentelemetry.propagators.textmap
import opentelemetry.trace
import opentelemetry.trace.propagation.tracecontext
import opentelemetry.util.types
from opentelemetry.context import Context
from opentelemetry.sdk.trace import IdGenerator, Span, RandomIdGenerator
from opentelemetry.trace import Status, StatusCode, get_current_span, Tracer, INVALID_SPAN_ID, INVALID_TRACE_ID
from typing_extensions import Protocol, TypedDict

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.converter
import temporalio.exceptions
import temporalio.worker
import temporalio.workflow
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

_ContextT = TypeVar("_ContextT", bound=nexusrpc.handler.OperationContext)


class TemporalIdGenerator(RandomIdGenerator):
    def generate_span_id(self) -> int:
        if temporalio.workflow.in_workflow():
            span_id = temporalio.workflow.random().getrandbits(64)
            while span_id == INVALID_SPAN_ID:
                span_id = temporalio.workflow.random().getrandbits(64)
            return span_id
        else:
            return super().generate_span_id()


    def generate_trace_id(self) -> int:
        if temporalio.workflow.in_workflow():
            trace_id = temporalio.workflow.random().getrandbits(128)
            while trace_id == INVALID_TRACE_ID:
                trace_id = temporalio.workflow.random().getrandbits(128)
            return trace_id
        else:
            return super().generate_trace_id()


def _context_to_headers(
    headers: Mapping[str, temporalio.api.common.v1.Payload]
) -> Mapping[str, temporalio.api.common.v1.Payload]:
    carrier: _CarrierDict = {}
    default_text_map_propagator.inject(carrier)
    if carrier:
        headers = {
            **headers,
            "_tracer-data": temporalio.converter.PayloadConverter.default.to_payloads([carrier])[0],
        }
    return headers

def _context_to_nexus_headers(
    headers: Mapping[str, str]
) -> Mapping[str, str]:
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

_tracer_context_key = opentelemetry.context.create_key(
    "__temporal_opentelemetry_tracer"
)

def _headers_to_context(tracer: Tracer, headers: Mapping[str, temporalio.api.common.v1.Payload]) -> Context:
    context_header = headers.get("_tracer-data")
    print("Header:", context_header)
    if context_header:
        context_carrier: _CarrierDict = temporalio.converter.PayloadConverter.default.from_payloads(
            [context_header]
        )[0]
        print("Carrier:", context_carrier)

        context = default_text_map_propagator.extract(context_carrier)
    else:
        context = opentelemetry.context.Context()
    context = opentelemetry.context.set_value(_tracer_context_key, tracer, context)
    return context


def _nexus_headers_to_context(tracer: Tracer, headers: Mapping[str, str]) -> Context:
    context = default_text_map_propagator.extract(headers)
    context = opentelemetry.context.set_value(_tracer_context_key, tracer, context)
    return context

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
    ) -> None:
        """Initialize a OpenTelemetry tracing interceptor.
        """
        self.tracer = tracer

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
        return _TracingActivityInboundInterceptor(next, self.tracer)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[TracingWorkflowInboundInterceptor]:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.workflow_interceptor_class`.
        """
        class InterceptorWithState(TracingWorkflowInboundInterceptor):
            tracer = self.tracer

        return InterceptorWithState

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.intercept_nexus_operation`.
        """
        return _TracingNexusOperationInboundInterceptor(next, self.tracer)



class _TracingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self, next: temporalio.client.OutboundInterceptor, root: TracingInterceptor
    ) -> None:
        super().__init__(next)
        self.root = root

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        input.headers = _context_to_headers(input.headers)
        print("Setting headers in start workflow: ", input.headers)
        return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        input.headers = _context_to_headers(input.headers)
        return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        input.headers = _context_to_headers(input.headers)
        return await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        input.headers = _context_to_headers(input.headers)
        return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        input.headers = _context_to_headers(input.headers)
        return await super().start_update_with_start_workflow(input)


class _TracingActivityInboundInterceptor(temporalio.worker.ActivityInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        tracer: Tracer,
    ) -> None:
        super().__init__(next)
        self.tracer = tracer

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        context = _headers_to_context(self.tracer, input.headers)
        token = opentelemetry.context.attach(context)
        try:

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
    ) -> None:
        super().__init__(next)
        self.tracer = tracer

    @contextmanager
    def _top_level_context(
        self, input: _InputWithStringHeaders
    ) -> Iterator[None]:
        context = _nexus_headers_to_context(self.tracer, input.headers)
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
        with self._top_level_context(input.ctx):
            return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        with self._top_level_context(input.ctx):
            return await self.next.execute_nexus_operation_cancel(input)


class _InputWithHeaders(Protocol):
    headers: Mapping[str, temporalio.api.common.v1.Payload]


class _InputWithStringHeaders(Protocol):
    headers: Mapping[str, str] | None



class TracingWorkflowInboundInterceptor(temporalio.worker.WorkflowInboundInterceptor):
    """Tracing interceptor for workflow calls.

    See :py:class:`TracingInterceptor` docs on why one might want to subclass
    this class.
    """
    tracer = None


    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        """Initialize a tracing workflow interceptor."""
        super().__init__(next)

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
        with self._top_level_workflow_context(input):
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_signal`.
        """
        with self._top_level_workflow_context(input):
            await super().handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_query`.
        """
        # TODO: Handle query
        return await super().handle_query(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_update_validator`.
        """
        with self._top_level_workflow_context(input):
            super().handle_update_validator(input)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        """Implementation of
        :py:meth:`temporalio.worker.WorkflowInboundInterceptor.handle_update_handler`.
        """
        with self._top_level_workflow_context(input):
            return await super().handle_update_handler(input)

    @contextmanager
    def _top_level_workflow_context(
        self, input: _InputWithHeaders
    ) -> Iterator[None]:
        context = _headers_to_context(self.tracer, input.headers)
        token = opentelemetry.context.attach(context)
        print("Top Level Current Span:", get_current_span())
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
        root: TracingWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        input.headers = _context_to_headers(input.headers)
        super().continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        input.headers = _context_to_headers(input.headers)
        await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        input.headers = _context_to_headers(input.headers)
        await super().signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        input.headers = _context_to_headers(input.headers)
        return super().start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        input.headers = _context_to_headers(input.headers)
        return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        input.headers = _context_to_headers(input.headers)
        return super().start_local_activity(input)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        input.headers = _context_to_nexus_headers(input.headers)
        return await super().start_nexus_operation(input)


class workflow:
    """Contains static methods that are safe to call from within a workflow.

    .. warning::
        Using any other ``opentelemetry`` API could cause non-determinism.
    """

    def __init__(self) -> None:  # noqa: D107
        raise NotImplementedError

    @contextmanager
    def start_as_current_span(
        name: str,
    ) -> Iterator[Span]:
        tracer: Tracer = cast(Tracer, opentelemetry.context.get_value(_tracer_context_key))
        with tracer.start_as_current_span(name, start_time=temporalio.workflow.time_ns()) as span:
            yield span