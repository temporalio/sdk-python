"""Debug plugin replayers."""

from typing import Any, Coroutine
from temporalio.worker import (
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)
from temporalio.worker._interceptor import ExecuteWorkflowInput, HandleSignalInput, StartActivityInput, StartChildWorkflowInput, StartLocalActivityInput

from temporalio import workflow

import os

import temporalio.bridge.temporal_sdk_bridge.DebugClient as DebugClient # pretend that i can get this for now until core pr

class DebugReplayer:

    client: DebugClient
    last_notified_start_event: int

    @staticmethod
    def start_debug_replayer(workflow_path: str):
        debugger_url = os.environ.get("TEMPORAL_DEBUGGER_PLUGIN_URL") # process.env.TEMPORAL_DEBUGGER_PLUGIN_URL
        DebugReplayer.client = DebugClient(debugger_url)
        # get history if needed
        DebugReplayer.last_notified_start_event = -1

    @staticmethod
    def _post_hold(event_id: int):
        try:
            DebugReplayer.client.post_wft_started(event_id)
        except:
            raise


# Interceptors

class _WFInboundInterceptors(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        event_id = workflow.info().get_current_history_length()
        DebugReplayer.client.post_wft_started(event_id)
        return await super().execute_workflow(input)
    
    def handle_signal(self, input: HandleSignalInput) -> Coroutine[Any, Any, None]:
        event_id = workflow.info().get_current_history_length()
        DebugReplayer.client.post_wft_started(event_id)
        return super().handle_signal(input)

class _WFOutboundInterceptors(WorkflowOutboundInterceptor):

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return super().start_activity(input)
        finally: 
            DebugReplayer.client.post_wft_started(event_id)

    def start_local_activity(self, input: StartLocalActivityInput) -> workflow.ActivityHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return super().start_local_activity(input)
        finally:
            DebugReplayer.client.post_wft_started(event_id)
            
    async def start_child_workflow(self, input: StartChildWorkflowInput) -> workflow.ChildWorkflowHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return await super().start_child_workflow(input)
        finally:
            DebugReplayer.client.post_wft_started(event_id)