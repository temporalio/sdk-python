"""Datadog tracing interceptor for Temporal activity inbound calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import temporalio.activity
import temporalio.worker
from temporalio.contrib.datadog._constants import (
    OperationNames,
    SpanAttributes,
)
from temporalio.contrib.datadog._id_generator import gen_span_id
from temporalio.contrib.datadog._span_runner import _SpanRunner

if TYPE_CHECKING:
    from temporalio.contrib.datadog._interceptor import DatadogTracingInterceptor


class _ActivityInboundInterceptor(  # type: ignore[reportUnsafeMultipleInheritance]
    _SpanRunner, temporalio.worker.ActivityInboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        root: DatadogTracingInterceptor,
    ) -> None:
        temporalio.worker.ActivityInboundInterceptor.__init__(self, next)
        _SpanRunner.__init__(self, root)

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        return await self.run(
            self._get_span(input),
            OperationNames.RUN_ACTIVITY,
            super().execute_activity(input),
        )

    def _get_span(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        info = temporalio.activity.info()
        return self.root.tracer.start_span(
            operation_name=OperationNames.RUN_ACTIVITY,
            parent_ctx=self.root.propagator.extract_headers(input.headers),
            resource_name=info.activity_type,
            activate=True,
            span_id=gen_span_id(
                f"{info.workflow_run_id}:{info.activity_id}:{info.attempt}"
            ),
            attributes=self._get_activity_attributes(info),
            parent_from_header=True,
        )

    @staticmethod
    def _get_activity_attributes(info: temporalio.activity.Info) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            SpanAttributes.ACTIVITY_ID: info.activity_id,
            SpanAttributes.ACTIVITY_TYPE: info.activity_type,
            SpanAttributes.ATTEMPT: info.attempt,
        }
        if info.workflow_id:
            attributes[SpanAttributes.WORKFLOW_ID] = info.workflow_id
        if info.workflow_run_id:
            attributes[SpanAttributes.RUN_ID] = info.workflow_run_id
        if info.workflow_namespace:
            attributes[SpanAttributes.NAMESPACE] = info.workflow_namespace
        return attributes
