from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Mapping, Protocol, Type

import temporalio.activity
import temporalio.api.common.v1
import temporalio.client
import temporalio.converter
import temporalio.worker
import temporalio.workflow
from agents import CustomSpanData, custom_span, get_current_span, trace
from agents.tracing import get_trace_provider
from agents.tracing.spans import NoOpSpan, SpanImpl
from temporalio import activity, workflow

HEADER_KEY = "__openai_span"


class _InputWithHeaders(Protocol):
    headers: Mapping[str, temporalio.api.common.v1.Payload]


def set_header_from_context(
    input: _InputWithHeaders, payload_converter: temporalio.converter.PayloadConverter
) -> None:
    current = get_current_span()
    if current is None or isinstance(current, NoOpSpan):
        return

    input.headers = {
        **input.headers,
        HEADER_KEY: payload_converter.to_payload(
            {
                "traceWorkflowName": get_trace_provider().get_current_trace().name,
                "spanId": current.span_id,
                "traceId": current.trace_id,
            }
        ),
    }


@contextmanager
def context_from_header(
    span_name: str,
    input: _InputWithHeaders,
    payload_converter: temporalio.converter.PayloadConverter,
):
    payload = input.headers.get(HEADER_KEY)
    span_info = payload_converter.from_payload(payload) if payload else None
    if span_info is None:
        yield
    else:
        span = SpanImpl(
            trace_id=span_info["traceId"] if span_info else None,
            span_id=span_info["spanId"] if span_info else None,
            parent_id=None,
            span_data=CustomSpanData(
                name="Parent Temporal Span",
                data={},
            ),
            processor=get_trace_provider()._multi_processor,
        )
        workflow_type = (
            activity.info().workflow_type
            if activity.in_activity()
            else workflow.info().workflow_type
        )
        data = (
            {"activityId": activity.info().activity_id}
            if activity.in_activity()
            else None
        )
        if get_trace_provider().get_current_trace() is None:
            metadata = {
                "temporal:workflowId": activity.info().workflow_id
                if activity.in_activity()
                else workflow.info().workflow_id,
                "temporal:runId": activity.info().workflow_run_id
                if activity.in_activity()
                else workflow.info().run_id,
                "temporal:workflowType": workflow_type,
            }
            with trace(
                span_info["traceWorkflowName"],
                trace_id=span_info["traceId"],
                metadata=metadata,
            ):
                with custom_span(name=span_name, parent=span, data=data):
                    yield
        else:
            with custom_span(name=span_name, parent=span, data=data):
                yield


class OpenAIAgentsTracingInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    """Interceptor that can serialize/deserialize contexts."""

    def __init__(
        self,
        payload_converter: temporalio.converter.PayloadConverter = temporalio.converter.default().payload_converter,
    ) -> None:
        self._payload_converter = payload_converter

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _ContextPropagationClientOutboundInterceptor(
            next, self._payload_converter
        )

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _ContextPropagationActivityInboundInterceptor(next)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> Type[_ContextPropagationWorkflowInboundInterceptor]:
        return _ContextPropagationWorkflowInboundInterceptor


class _ContextPropagationClientOutboundInterceptor(
    temporalio.client.OutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        payload_converter: temporalio.converter.PayloadConverter,
    ) -> None:
        super().__init__(next)
        self._payload_converter = payload_converter

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        metadata = {
            "temporal:workflowType": input.workflow,
            **({"temporal:workflowId": input.id} if input.id else {}),
        }
        data = {"workflowId": input.id} if input.id else None
        span_name = f"temporal:startWorkflow"
        if get_trace_provider().get_current_trace() is None:
            with trace(
                span_name + ":" + input.workflow, metadata=metadata, group_id=input.id
            ):
                with custom_span(name=span_name + ":" + input.workflow, data=data):
                    set_header_from_context(input, self._payload_converter)
                    return await super().start_workflow(input)
        else:
            with custom_span(name=span_name, data=data):
                set_header_from_context(input, self._payload_converter)
                return await super().start_workflow(input)

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        metadata = {
            "temporal:queryWorkflow": input.query,
            **({"temporal:workflowId": input.id} if input.id else {}),
        }
        data = {"workflowId": input.id, "query": input.query}
        span_name = f"temporal:queryWorkflow"
        if get_trace_provider().get_current_trace() is None:
            with trace(span_name, metadata=metadata, group_id=input.id):
                with custom_span(name=span_name, data=data):
                    set_header_from_context(input, self._payload_converter)
                    return await super().query_workflow(input)
        else:
            with custom_span(name=span_name, data=data):
                set_header_from_context(input, self._payload_converter)
                return await super().query_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        metadata = {
            "temporal:signalWorkflow": input.signal,
            **({"temporal:workflowId": input.id} if input.id else {}),
        }
        data = {"workflowId": input.id, "signal": input.signal}
        span_name = f"temporal:signalWorkflow"
        if get_trace_provider().get_current_trace() is None:
            with trace(span_name, metadata=metadata, group_id=input.id):
                with custom_span(name=span_name, data=data):
                    set_header_from_context(input, self._payload_converter)
                    await super().signal_workflow(input)
        else:
            with custom_span(name=span_name, data=data):
                set_header_from_context(input, self._payload_converter)
                await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        metadata = {
            "temporal:updateWorkflow": input.update,
            **({"temporal:workflowId": input.id} if input.id else {}),
        }
        data = {
            **({"workflowId": input.id} if input.id else {}),
            "update": input.update,
        }
        span_name = "temporal:updateWorkflow"
        if get_trace_provider().get_current_trace() is None:
            with trace(span_name, metadata=metadata, group_id=input.id):
                with custom_span(name=span_name, data=data):
                    set_header_from_context(input, self._payload_converter)
                    return await self.next.start_workflow_update(input)
        else:
            with custom_span(name=span_name, data=data):
                set_header_from_context(input, self._payload_converter)
                return await self.next.start_workflow_update(input)


class _ContextPropagationActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor
):
    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        with context_from_header(
            "temporal:executeActivity", input, temporalio.activity.payload_converter()
        ):
            return await self.next.execute_activity(input)


class _ContextPropagationWorkflowInboundInterceptor(
    temporalio.worker.WorkflowInboundInterceptor
):
    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        self.next.init(_ContextPropagationWorkflowOutboundInterceptor(outbound))

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        with context_from_header(
            "temporal:executeWorkflow", input, temporalio.workflow.payload_converter()
        ):
            return await self.next.execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with context_from_header(
            "temporal:handleSignal", input, temporalio.workflow.payload_converter()
        ):
            return await self.next.handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        with context_from_header(
            "temporal:handleQuery", input, temporalio.workflow.payload_converter()
        ):
            return await self.next.handle_query(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        with context_from_header(
            "temporal:handleUpdateValidator",
            input,
            temporalio.workflow.payload_converter(),
        ):
            self.next.handle_update_validator(input)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        with context_from_header(
            "temporal:handleUpdateHandler",
            input,
            temporalio.workflow.payload_converter(),
        ):
            return await self.next.handle_update_handler(input)


class _ContextPropagationWorkflowOutboundInterceptor(
    temporalio.worker.WorkflowOutboundInterceptor
):
    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        set_header_from_context(input, temporalio.workflow.payload_converter())
        return await self.next.signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        set_header_from_context(input, temporalio.workflow.payload_converter())
        return await self.next.signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        with custom_span(
            name=f"temporal:startActivity:{input.activity}",
        ):
            set_header_from_context(input, temporalio.workflow.payload_converter())
            return self.next.start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        set_header_from_context(input, temporalio.workflow.payload_converter())
        return await self.next.start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        set_header_from_context(input, temporalio.workflow.payload_converter())
        return self.next.start_local_activity(input)
