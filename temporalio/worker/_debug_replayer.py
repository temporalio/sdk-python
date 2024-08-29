"""Debug plugin replayers."""

import os
from typing import Any, Coroutine, Sequence, Type

from google.protobuf.json_format import MessageToDict

import temporalio.bridge.temporal_sdk_bridge.DebugClient as DebugClient
from temporalio import client, workflow
from temporalio.worker import (
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)
from temporalio.worker._interceptor import (
    ExecuteWorkflowInput,
    HandleSignalInput,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    WorkflowInterceptorClassInput,
)

from ._replayer import Replayer


class DebugReplayer:
    client: DebugClient
    last_notified_start_event: int

    @staticmethod
    async def start_debug_replayer(workflows: Sequence[Type]):
        debugger_url = os.environ.get(
            "TEMPORAL_DEBUGGER_PLUGIN_URL"
        )  # process.env.TEMPORAL_DEBUGGER_PLUGIN_URL
        DebugReplayer.client = DebugClient(debugger_url)
        # get history if needed
        DebugReplayer.last_notified_start_event = -1

        # create replayer
        interceptors = [_Interceptor]
        replayer = Replayer(workflows=workflows, interceptors=interceptors)
        history = DebugReplayer.client.history
        wf_history = client.WorkflowHistory.from_json(
            history=MessageToDict(history),
            workflow_id="debug-replay-wf",
        )  # this is stupid
        await replayer.replay_workflow(wf_history, raise_on_replay_failure=True)

    @staticmethod
    def _post_hold(event_id: int):
        try:
            DebugReplayer.client.post_wft_started(event_id)
        except:
            raise


# Interceptors
class _Interceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> WorkflowInboundInterceptor | None:
        return _WFInboundInterceptors.init(outbound=_WFOutboundInterceptors)


class _WFInboundInterceptors(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        return super().init(_WFOutboundInterceptors(outbound))

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

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> workflow.ActivityHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return super().start_local_activity(input)
        finally:
            DebugReplayer.client.post_wft_started(event_id)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return await super().start_child_workflow(input)
        finally:
            DebugReplayer.client.post_wft_started(event_id)
