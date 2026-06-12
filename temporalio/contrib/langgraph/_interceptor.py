"""Workflow interceptor that scopes LangGraph graphs/entrypoints to the workflow run."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from temporalio import workflow
from temporalio.contrib.langgraph._activity import clear_store_warning
from temporalio.contrib.workflow_streams._stream import _PUBLISH_SIGNAL
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

_workflow_graphs: dict[str, dict[str, StateGraph[Any, Any, Any, Any]]] = {}
_workflow_entrypoints: dict[str, dict[str, Pregel[Any, Any, Any, Any]]] = {}


class LangGraphInterceptor(Interceptor):
    """Interceptor that registers a workflow's graphs and entrypoints for the run."""

    def __init__(
        self,
        graphs: dict[str, StateGraph[Any, Any, Any, Any]],
        entrypoints: dict[str, Pregel[Any, Any, Any, Any]],
        streaming_topic: str | None = None,
    ) -> None:
        """Initialize with the graphs and entrypoints to scope to each workflow run."""
        self._graphs = graphs
        self._entrypoints = entrypoints
        self._streaming_topic = streaming_topic

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        """Return the inbound interceptor class used to scope graphs per run."""
        graphs = self._graphs
        entrypoints = self._entrypoints
        streaming_topic = self._streaming_topic

        class Inbound(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                run_id = outbound.info().run_id
                _workflow_graphs[run_id] = graphs
                _workflow_entrypoints[run_id] = entrypoints
                super().init(outbound)

            async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
                if (
                    streaming_topic is not None
                    and workflow.get_signal_handler(_PUBLISH_SIGNAL) is None
                ):
                    raise RuntimeError(
                        f"LangGraphPlugin was configured with "
                        f"streaming_topic={streaming_topic!r}, but workflow "
                        f"{workflow.info().workflow_type!r} did not register a "
                        f"WorkflowStream. Construct WorkflowStream() in the "
                        f"workflow's @workflow.init (i.e. __init__) method so "
                        f"streaming activities can publish to it."
                    )
                try:
                    return await self.next.execute_workflow(input)
                finally:
                    run_id = workflow.info().run_id
                    _workflow_graphs.pop(run_id, None)
                    _workflow_entrypoints.pop(run_id, None)
                    clear_store_warning(run_id)

        return Inbound
