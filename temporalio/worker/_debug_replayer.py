"""Debug plugin replayers."""

import inspect
import os
from typing import Any, Coroutine, Sequence, Type

from google.protobuf.json_format import MessageToDict
import asyncio
from temporalio import client, workflow
from temporalio.bridge.temporal_sdk_bridge import DebugClient
from temporalio.worker._interceptor import (
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    SignalChildWorkflowInput,
    SignalExternalWorkflowInput,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    WorkflowInterceptorClassInput,
)

from ._interceptor import (
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)
from ._replayer import Replayer


class DebugReplayer:
    """A wrapper for functions used to interface with the VSCode debug plugin."""

    client: DebugClient
    last_notified_start_event: int

    @staticmethod
    def start_debug_replayer(workflows: Sequence[Type]):
        """Start the debug replayer with the given set of workflows."""
        debugger_url = os.environ.get(
            "TEMPORAL_DEBUGGER_PLUGIN_URL"
        )  # process.env.TEMPORAL_DEBUGGER_PLUGIN_URL
        DebugReplayer.client = DebugClient(debugger_url)
        # get history if needed
        DebugReplayer.last_notified_start_event = -1

        # create replayer
        interceptors = [_Interceptor()]
        replayer = Replayer(workflows=workflows, interceptors=interceptors)
        history = DebugReplayer.client.history
        wf_history = client.WorkflowHistory.from_json(
            history=MessageToDict(history),
            workflow_id="debug-replay-wf",
        )  # this is stupid
        asyncio.run(replayer.replay_workflow(wf_history, raise_on_replay_failure=True))

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
    ) -> Type[WorkflowInboundInterceptor] | None:
        return _WFInboundInterceptors


class _WFInboundInterceptors(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        return super().init(_WFOutboundInterceptors(outbound))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        event_id = workflow.info().get_current_history_length()
        st = inspect.stack()[0][3]
        print(f"sending message from {st}")
        DebugReplayer.client.post_wft_started(event_id)
        return await super().execute_workflow(input)

    def handle_signal(self, input: HandleSignalInput) -> Coroutine[Any, Any, None]:
        event_id = workflow.info().get_current_history_length()
        st = inspect.stack()[0][3]
        print(f"sending message from {st}")
        DebugReplayer.client.post_wft_started(event_id)
        return super().handle_signal(input)

    def handle_query(self, input: HandleQueryInput) -> Coroutine[Any, Any, Any]:
        event_id = workflow.info().get_current_history_length()
        st = inspect.stack()[0][3]
        print(f"sending message from {st}")
        DebugReplayer.client.post_wft_started(event_id)
        return super().handle_query(input)


class _WFOutboundInterceptors(WorkflowOutboundInterceptor):
    async def signal_child_workflow(self, input: SignalChildWorkflowInput) -> None:
        try:
            event_id = workflow.info().get_current_history_length()
            return await super().signal_child_workflow(input)
        finally:
            st = inspect.stack()[0][3]
            print(f"sending message from {st}")
            DebugReplayer.client.post_wft_started(event_id)

    async def signal_external_workflow(
        self, input: SignalExternalWorkflowInput
    ) -> None:
        try:
            event_id = workflow.info().get_current_history_length()
            return await super().signal_external_workflow(input)
        finally:
            st = inspect.stack()[0][3]
            print(f"sending message from {st}")
            DebugReplayer.client.post_wft_started(event_id)

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return super().start_activity(input)
        finally:
            st = inspect.stack()[0][3]
            print(f"sending message from {st}")
            DebugReplayer.client.post_wft_started(event_id)

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> workflow.ActivityHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return super().start_local_activity(input)
        finally:
            st = inspect.stack()[0][3]
            print(f"sending message from {st}")
            DebugReplayer.client.post_wft_started(event_id)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        try:
            event_id = workflow.info().get_current_history_length()
            return await super().start_child_workflow(input)
        finally:
            st = inspect.stack()[0][3]
            print(f"sending message from {st}")
            DebugReplayer.client.post_wft_started(event_id)
