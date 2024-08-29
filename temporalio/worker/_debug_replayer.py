"""Debug plugin replayers."""

from typing import Any
import temporalio.workflow as workflow
from temporalio.worker import (
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
    ActivityOutboundInterceptor,
    ActivityInboundInterceptor,
)
from temporalio.worker._interceptor import ExecuteWorkflowInput

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


    # Interceptors

    class _InboundInterceptor(WorkflowInboundInterceptor):
        async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
            # steps:
            # get workflow event id
            # check if workflow event type is one we should pause on
            # send post request via client, wait for response
            DebugReplayer.client.post_wft_started(input.headers.get("eventId")) # TODO eventid
            return await super().execute_workflow(input)