"""Workflow interceptor for the LangGraph plugin."""

import asyncio
from typing import Any

from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)


class LangGraphInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _LangGraphWorkflowInboundInterceptor


class _LangGraphWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Patches the workflow event loop so LangGraph's `asyncio.eager_task_factory`
    (which calls `loop.is_running()`) works inside Temporal's sandbox."""

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        loop = asyncio.get_event_loop()
        setattr(loop, "is_running", lambda: True)
        return await super().execute_workflow(input)
