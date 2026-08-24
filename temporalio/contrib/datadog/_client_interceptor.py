"""Datadog tracing interceptor for Temporal client outbound calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import temporalio.client
from temporalio.contrib.datadog._constants import (
    COMMON_ATTRIBUTE_MAP,
    OperationNames,
    SpanAttributes,
)
from temporalio.contrib.datadog._span_runner import _SpanRunner

if TYPE_CHECKING:
    from temporalio.contrib.datadog._interceptor import DatadogTracingInterceptor


class _ClientOutboundInterceptor(  # type: ignore[reportUnsafeMultipleInheritance]
    _SpanRunner, temporalio.client.OutboundInterceptor
):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        root: DatadogTracingInterceptor,
    ) -> None:
        temporalio.client.OutboundInterceptor.__init__(self, next)
        _SpanRunner.__init__(self, root)

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        operation_name = (
            OperationNames.SIGNAL_WITH_START_WORKFLOW
            if input.start_signal
            else OperationNames.START_WORKFLOW
        )
        return await self._start_operation(
            operation_name,
            input,
            input.workflow,
            self._get_workflow_attributes(input),
            super().start_workflow,
        )

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        if self.root.workflow_tracing_config.disable_signal_tracing:
            return await super().signal_workflow(input)
        return await self._start_operation(
            OperationNames.SIGNAL_WORKFLOW,
            input,
            input.signal,
            self._get_workflow_attributes(input),
            super().signal_workflow,
        )

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        if self.root.workflow_tracing_config.disable_query_tracing:
            return await super().query_workflow(input)
        return await self._start_operation(
            OperationNames.QUERY_WORKFLOW,
            input,
            input.query,
            self._get_workflow_attributes(input),
            super().query_workflow,
        )

    async def create_schedule(
        self, input: temporalio.client.CreateScheduleInput
    ) -> temporalio.client.ScheduleHandle:
        span = self._get_span(OperationNames.CREATE_SCHEDULE, input.id)
        return await self.run(
            span, OperationNames.CREATE_SCHEDULE, super().create_schedule(input)
        )

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        if self.root.workflow_tracing_config.disable_update_tracing:
            return await super().start_workflow_update(input)
        return await self._start_operation(
            OperationNames.UPDATE_WORKFLOW,
            input,
            input.update,
            self._get_workflow_attributes(input),
            super().start_workflow_update,
        )

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        if self.root.workflow_tracing_config.disable_update_tracing:
            # Update tracing is disabled, but this call also starts a new workflow.
            # Propagate the currently active trace into the workflow start headers so
            # RunWorkflow is not an unparented root when workflow tracing is enabled.
            active_ctx = self.root.tracer.tracer.context_provider.active()
            input.start_workflow_input.headers = self.root.propagator.inject_headers(
                input.start_workflow_input.headers, active_ctx
            )
            return await super().start_update_with_start_workflow(input)

        operation_name = OperationNames.UPDATE_WITH_START_WORKFLOW
        span = self._get_span(
            operation_name,
            input.start_workflow_input.workflow,
            self._get_workflow_attributes(input.start_workflow_input),
        )

        input.start_workflow_input.headers = self.root.propagator.inject_headers(
            input.start_workflow_input.headers, span.context
        )
        input.update_workflow_input.headers = self.root.propagator.inject_headers(
            input.update_workflow_input.headers, span.context
        )

        return await self.run(
            span,
            operation_name,
            super().start_update_with_start_workflow(input),
        )

    async def start_activity(
        self, input: temporalio.client.StartActivityInput
    ) -> temporalio.client.ActivityHandle[Any]:
        return await self._start_operation(
            OperationNames.START_ACTIVITY,
            input,
            input.activity_type,
            self._get_activity_attributes(input),
            super().start_activity,
        )

    async def _start_operation(
        self,
        operation_name: str,
        input: Any,
        resource_name: str,
        attributes: dict[str, Any],
        awaitable: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        span = self._get_span(operation_name, resource_name, attributes)
        input.headers = self.root.propagator.inject_headers(input.headers, span.context)
        return await self.run(span, operation_name, awaitable(input))

    def _get_span(
        self,
        operation_name: str,
        resource_name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        # Use the currently active ddtrace span as parent if one exists
        parent_ctx = self.root.tracer.tracer.context_provider.active()
        return self.root.tracer.start_span(
            operation_name=operation_name,
            parent_ctx=parent_ctx,
            resource_name=resource_name,
            activate=True,
            attributes=attributes,
            parent_from_header=False,
        )

    @classmethod
    def _get_workflow_attributes(cls, input: Any) -> dict[str, Any]:
        attributes: dict[str, Any] = {SpanAttributes.WORKFLOW_ID: input.id}
        for field, span_key in COMMON_ATTRIBUTE_MAP:
            if val := getattr(input, field, None):
                attributes[span_key] = val
        if getattr(input, "workflow", None):
            attributes[SpanAttributes.WORKFLOW_TYPE] = input.workflow
        if getattr(input, "update", None):
            attributes[SpanAttributes.UPDATE_NAME] = input.update
        if getattr(input, "update_id", None):
            attributes[SpanAttributes.UPDATE_ID] = input.update_id
        return attributes

    @classmethod
    def _get_activity_attributes(cls, input: Any) -> dict[str, Any]:
        return {
            SpanAttributes.ACTIVITY_ID: input.id,
            SpanAttributes.ACTIVITY_TYPE: input.activity_type,
        }
