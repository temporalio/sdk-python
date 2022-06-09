import asyncio
import uuid
from datetime import timedelta
from typing import Any, Callable, List, NoReturn, Optional, Tuple, Type

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ContinueAsNewInput,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    GetExternalWorkflowHandleInput,
    HandleQueryInput,
    HandleSignalInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)

interceptor_traces: List[Tuple[str, Any]] = []


class TracingWorkerInterceptor(Interceptor):
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return TracingActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(self) -> Optional[Type[WorkflowInboundInterceptor]]:
        return TracingWorkflowInboundInterceptor


class TracingActivityInboundInterceptor(ActivityInboundInterceptor):
    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        super().init(TracingActivityOutboundInterceptor(outbound))

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        interceptor_traces.append(("activity.execute", input))
        return await super().execute_activity(input)


class TracingActivityOutboundInterceptor(ActivityOutboundInterceptor):
    def info(self) -> activity.Info:
        interceptor_traces.append(("activity.info", super().info()))
        return super().info()

    def heartbeat(self, *details: Any) -> None:
        interceptor_traces.append(("activity.heartbeat", details))
        super().heartbeat(*details)


class TracingWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(TracingWorkflowOutboundInterceptor(outbound))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        interceptor_traces.append(("workflow.execute", input))
        return await super().execute_workflow(input)

    async def handle_signal(self, input: HandleSignalInput) -> None:
        interceptor_traces.append(("workflow.signal", input))
        return await super().handle_signal(input)

    async def handle_query(self, input: HandleQueryInput) -> Any:
        interceptor_traces.append(("workflow.query", input))
        return await super().handle_query(input)


class TracingWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        interceptor_traces.append(("workflow.continue_as_new", input))
        super().continue_as_new(input)

    def get_external_workflow_handle(
        self, input: GetExternalWorkflowHandleInput
    ) -> workflow.ExternalWorkflowHandle:
        interceptor_traces.append(("workflow.get_external_workflow_handle", input))
        return super().get_external_workflow_handle(input)

    def info(self) -> workflow.Info:
        interceptor_traces.append(("workflow.info", super().info()))
        return super().info()

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        interceptor_traces.append(("workflow.start_activity", input))
        return super().start_activity(input)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        interceptor_traces.append(("workflow.start_child_workflow", input))
        return await super().start_child_workflow(input)

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> workflow.ActivityHandle:
        interceptor_traces.append(("workflow.start_local_activity", input))
        return super().start_local_activity(input)


@activity.defn
async def intercepted_activity(param: str) -> str:
    if not activity.info().is_local:
        activity.heartbeat("details")
    return f"param: {param}"


@workflow.defn
class InterceptedWorkflow:
    def __init__(self) -> None:
        self.finish = asyncio.Event()

    @workflow.run
    async def run(self, initial_run: bool) -> None:
        if not initial_run:
            return
        await workflow.execute_activity(
            intercepted_activity, "val1", schedule_to_close_timeout=timedelta(seconds=5)
        )
        await workflow.execute_local_activity(
            intercepted_activity, "val2", schedule_to_close_timeout=timedelta(seconds=5)
        )
        await workflow.execute_child_workflow(
            InterceptedWorkflow.run, False, id=f"{workflow.info().workflow_id}_child"
        )
        workflow.get_external_workflow_handle("some-id")
        await self.finish.wait()
        workflow.continue_as_new(False)

    @workflow.query
    def query(self, param: str) -> str:
        return f"query: {param}"

    @workflow.signal
    def signal(self, param: str) -> None:
        self.finish.set()


async def test_worker_interceptor(client: Client):
    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterceptedWorkflow],
        activities=[intercepted_activity],
        interceptors=[TracingWorkerInterceptor()],
    ):
        # Run workflow
        handle = await client.start_workflow(
            InterceptedWorkflow.run,
            True,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=task_queue,
        )
        assert "query: query-val" == await handle.query(
            InterceptedWorkflow.query, "query-val"
        )
        await handle.signal(InterceptedWorkflow.signal, "signal-val")
        await handle.result()

        # Check traces
        def pop_trace(name: str, filter: Optional[Callable[[Any], bool]] = None) -> Any:
            index = next(
                (
                    i
                    for i, v in enumerate(interceptor_traces)
                    if v[0] == name and (not filter or filter(v[1]))
                ),
                None,
            )
            if index is None:
                return None
            return interceptor_traces.pop(index)[1]

        assert pop_trace("activity.execute", lambda v: v.args[0] == "val1")
        assert pop_trace("activity.execute", lambda v: v.args[0] == "val2")
        # Check activity info is called _at least_ twice, but is called more
        # because the logger uses it for context
        activity_infos = 0
        while pop_trace("activity.info"):
            activity_infos += 1
        assert activity_infos >= 2
        assert pop_trace("activity.heartbeat", lambda v: v[0] == "details")
        # One initial, one child, one continue as new
        assert pop_trace("workflow.execute", lambda v: v.args[0] is True)
        assert pop_trace("workflow.execute", lambda v: v.args[0] is False)
        assert pop_trace("workflow.execute", lambda v: v.args[0] is False)
        assert pop_trace("workflow.signal", lambda v: v.args[0] == "signal-val")
        assert pop_trace("workflow.query", lambda v: v.args[0] == "query-val")
        assert pop_trace("workflow.continue_as_new", lambda v: v.args[0] is False)
        assert pop_trace(
            "workflow.get_external_workflow_handle", lambda v: v.id == "some-id"
        )
        assert pop_trace("workflow.info")
        assert pop_trace("workflow.start_activity", lambda v: v.args[0] == "val1")
        assert pop_trace("workflow.start_child_workflow", lambda v: v.args[0] is False)
        assert pop_trace("workflow.start_local_activity", lambda v: v.args[0] == "val2")
        # Confirm no unexpected traces
        assert not interceptor_traces
