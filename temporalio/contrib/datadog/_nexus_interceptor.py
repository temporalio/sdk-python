"""Datadog tracing interceptor for Temporal Nexus operation inbound calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nexusrpc.handler

import temporalio.worker
from temporalio.contrib.datadog._constants import OperationNames, SpanAttributes
from temporalio.contrib.datadog._span_runner import _SpanRunner

if TYPE_CHECKING:
    from temporalio.contrib.datadog._interceptor import DatadogTracingInterceptor


class _NexusOperationInboundInterceptor(  # type: ignore[reportUnsafeMultipleInheritance]
    _SpanRunner, temporalio.worker.NexusOperationInboundInterceptor
):
    def __init__(
        self,
        next: temporalio.worker.NexusOperationInboundInterceptor,
        root: DatadogTracingInterceptor,
    ) -> None:
        temporalio.worker.NexusOperationInboundInterceptor.__init__(self, next)
        _SpanRunner.__init__(self, root)

    async def execute_nexus_operation_start(
        self, input: temporalio.worker.ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any]
        | nexusrpc.handler.StartOperationResultAsync
    ):
        return await self.run(
            self._get_span(input, OperationNames.RUN_NEXUS_OPERATION_START_HANDLER),
            OperationNames.RUN_NEXUS_OPERATION_START_HANDLER,
            super().execute_nexus_operation_start(input),
        )

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        return await self.run(
            self._get_span(input, OperationNames.RUN_NEXUS_OPERATION_CANCEL_HANDLER),
            OperationNames.RUN_NEXUS_OPERATION_CANCEL_HANDLER,
            super().execute_nexus_operation_cancel(input),
        )

    def _get_span(self, input: Any, operation_name: str) -> Any:
        return self.root.tracer.start_span(
            operation_name=operation_name,
            parent_ctx=self.root.propagator.extract(input.ctx.headers),
            resource_name=f"{input.ctx.service}/{input.ctx.operation}",
            activate=True,
            attributes=self._get_nexus_attributes(input.ctx),
            parent_from_header=True,
        )

    @staticmethod
    def _get_nexus_attributes(nexus_ctx: Any) -> dict[str, Any]:
        return {
            SpanAttributes.NEXUS_SERVICE: nexus_ctx.service,
            SpanAttributes.NEXUS_OPERATION: nexus_ctx.operation,
        }
