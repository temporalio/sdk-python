"""VSCode Debug plugin helpers."""

import os
from typing import Any, Optional, Sequence, Type

import temporalio.api.history.v1
from temporalio import client, runtime, workflow
from temporalio.bridge.temporal_sdk_bridge import DebugClient, new_debug_client
from temporalio.worker._interceptor import (
    ExecuteWorkflowInput,
    HandleSignalInput,
    Interceptor,
    SignalChildWorkflowInput,
    SignalExternalWorkflowInput,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from temporalio.worker._replayer import Replayer


class DebugReplayer:
    client: DebugClient

    @staticmethod
    async def start_debug_replayer(workflows: Sequence[Type]):
        """Start the debug replayer with the given set of workflows."""
        debugger_url = os.environ.get("TEMPORAL_DEBUGGER_PLUGIN_URL")
        rt = runtime.Runtime.default()
        DebugReplayer.client = await new_debug_client(
            debugger_url=debugger_url,
            runtime_ref=rt._core_runtime._ref,
        )

        interceptors = [_Interceptor()]
        replayer = Replayer(
            workflows=workflows, interceptors=interceptors, runtime=rt, debug_mode=True
        )
        history_bytes = DebugReplayer.client.get_history()
        history = temporalio.api.history.v1.History()
        history.ParseFromString(history_bytes)
        wf_history = client.WorkflowHistory.from_proto(
            history=history,
            workflow_id="debug-replay-wf",
        )
        await replayer.replay_workflow(wf_history, raise_on_replay_failure=True)

    @staticmethod
    async def notify_from_workflow():
        event_id = workflow.info().get_current_history_length()
        print(f"notifying at event id {event_id}")
        await DebugReplayer.client.post_wft_started(event_id)


# Interceptors
class _Interceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        return _WFInboundInterceptors


class _WFInboundInterceptors(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        return super().init(_WFOutboundInterceptors(outbound))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        print("Executing")
        await DebugReplayer.notify_from_workflow()
        res = await super().execute_workflow(input)
        print("Executed")
        return res

    async def handle_signal(self, input: HandleSignalInput) -> None:
        print(f"handling signal {input}")
        await DebugReplayer.notify_from_workflow()
        await super().handle_signal(input)
        print(f"handling signal {input}")


class _WFOutboundInterceptors(WorkflowOutboundInterceptor):
    async def signal_child_workflow(self, input: SignalChildWorkflowInput) -> None:
        try:
            return await super().signal_child_workflow(input)
        finally:
            await DebugReplayer.notify_from_workflow()

    async def signal_external_workflow(
        self, input: SignalExternalWorkflowInput
    ) -> None:
        try:
            return await super().signal_external_workflow(input)
        finally:
            await DebugReplayer.notify_from_workflow()

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        try:
            return super().start_activity(input)
        finally:
            # TODO: Ugh should be async but can't be
            DebugReplayer.notify_from_workflow()

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> workflow.ActivityHandle:
        try:
            return super().start_local_activity(input)
        finally:
            # TODO: Ugh should be async but can't be
            DebugReplayer.notify_from_workflow()

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        try:
            return await super().start_child_workflow(input)
        finally:
            await DebugReplayer.notify_from_workflow()
