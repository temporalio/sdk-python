import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, NoReturn

import nexusrpc
import pytest

from temporalio import activity, nexus, workflow
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError, NexusOperationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ContinueAsNewInput,
    ExecuteActivityInput,
    ExecuteNexusOperationCancelInput,
    ExecuteNexusOperationStartInput,
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    HandleUpdateInput,
    Interceptor,
    NexusOperationInboundInterceptor,
    SignalChildWorkflowInput,
    SignalExternalWorkflowInput,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    StartNexusOperationInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers.nexus import make_nexus_endpoint_name

interceptor_traces: list[tuple[str, Any]] = []


class TracingWorkerInterceptor(Interceptor):
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return TracingActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return TracingWorkflowInboundInterceptor

    def intercept_nexus_operation(
        self, next: NexusOperationInboundInterceptor
    ) -> NexusOperationInboundInterceptor:
        return TracingNexusInboundInterceptor(next)


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

    def handle_update_validator(self, input: HandleUpdateInput) -> None:
        interceptor_traces.append(("workflow.update.validator", input))
        return super().handle_update_validator(input)

    async def handle_update_handler(self, input: HandleUpdateInput) -> Any:
        interceptor_traces.append(("workflow.update.handler", input))
        return await super().handle_update_handler(input)


class TracingWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        interceptor_traces.append(("workflow.continue_as_new", input))
        super().continue_as_new(input)

    def info(self) -> workflow.Info:
        interceptor_traces.append(("workflow.info", super().info()))
        return super().info()

    async def signal_child_workflow(self, input: SignalChildWorkflowInput) -> None:
        interceptor_traces.append(("workflow.signal_child_workflow", input))
        await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: SignalExternalWorkflowInput
    ) -> None:
        interceptor_traces.append(("workflow.signal_external_workflow", input))
        await super().signal_external_workflow(input)

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

    async def start_nexus_operation(
        self, input: StartNexusOperationInput
    ) -> workflow.NexusOperationHandle:
        interceptor_traces.append(("workflow.start_nexus_operation", input))
        return await super().start_nexus_operation(input)


class TracingNexusInboundInterceptor(NexusOperationInboundInterceptor):
    async def execute_nexus_operation_start(
        self, input: ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any]
        | nexusrpc.handler.StartOperationResultAsync
    ):
        interceptor_traces.append(
            (f"nexus.start_operation.{input.ctx.service}.{input.ctx.operation}", input)
        )
        return await super().execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: ExecuteNexusOperationCancelInput
    ) -> None:
        interceptor_traces.append(
            (f"nexus.cancel_operation.{input.ctx.service}.{input.ctx.operation}", input)
        )
        return await super().execute_nexus_operation_cancel(input)


@workflow.defn
class ExpectCancelNexusWorkflow:
    @workflow.run
    async def run(self, _input: str):
        try:
            await asyncio.wait_for(asyncio.Future(), 2)
        except asyncio.TimeoutError:
            raise ApplicationError("expected cancellation")


@nexusrpc.handler.service_handler
class InterceptedNexusService:
    @nexus.workflow_run_operation
    async def intercepted_operation(
        self, ctx: nexus.WorkflowRunOperationContext, input: str
    ) -> nexus.WorkflowHandle[None]:
        return await ctx.start_workflow(
            ExpectCancelNexusWorkflow.run,
            input,
            id=f"wf-{uuid.uuid4()}-{ctx.request_id}",
        )


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
    async def run(self, style: str) -> None:
        if style == "continue-as-new":
            return
        if style == "child" or style == "external":
            await self.finish.wait()
            return

        await workflow.execute_activity(
            intercepted_activity, "val1", schedule_to_close_timeout=timedelta(seconds=5)
        )
        await workflow.execute_local_activity(
            intercepted_activity, "val2", schedule_to_close_timeout=timedelta(seconds=5)
        )
        my_id = workflow.info().workflow_id
        child_handle = await workflow.start_child_workflow(
            InterceptedWorkflow.run, "child", id=f"{my_id}_child"
        )
        await child_handle.signal(InterceptedWorkflow.signal, "child-signal-val")
        await child_handle
        # Create another child so we can use it for external handle
        child_handle = await workflow.start_child_workflow(
            InterceptedWorkflow.run, "external", id=f"{my_id}_external"
        )
        await workflow.get_external_workflow_handle(child_handle.id).signal(
            InterceptedWorkflow.signal, "external-signal-val"
        )
        await child_handle

        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=InterceptedNexusService,
        )

        nexus_handle = await nexus_client.start_operation(
            operation=InterceptedNexusService.intercepted_operation,
            input="nexus-workflow",
        )
        nexus_handle.cancel()

        try:
            await nexus_handle
        except NexusOperationError:
            pass

        await self.finish.wait()
        workflow.continue_as_new("continue-as-new")

    @workflow.query
    def query(self, param: str) -> str:
        return f"query: {param}"

    @workflow.signal
    def signal(self, _param: str) -> None:
        self.finish.set()

    @workflow.update
    def update(self, param: str) -> str:
        return f"update: {param}"

    @workflow.update
    def update_validated(self, param: str) -> str:
        return f"update: {param}"

    @update_validated.validator
    def update_validated_validator(self, param: str) -> None:
        if param == "reject-me":
            raise ApplicationError("Invalid update")


async def test_worker_interceptor(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    task_queue = f"task-queue-{uuid.uuid4()}"
    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterceptedWorkflow, ExpectCancelNexusWorkflow],
        activities=[intercepted_activity],
        interceptors=[TracingWorkerInterceptor()],
        nexus_service_handlers=[InterceptedNexusService()],
    ):
        # Run workflow
        handle = await client.start_workflow(
            InterceptedWorkflow.run,
            "initial",
            id=f"workflow_{uuid.uuid4()}",
            task_queue=task_queue,
        )
        assert "query: query-val" == await handle.query(
            InterceptedWorkflow.query, "query-val"
        )
        await handle.signal(InterceptedWorkflow.signal, "signal-val")
        assert "update: update-val" == await handle.execute_update(
            InterceptedWorkflow.update, "update-val"
        )
        with pytest.raises(WorkflowUpdateFailedError) as _err:
            await handle.execute_update(
                InterceptedWorkflow.update_validated, "reject-me"
            )
        await handle.result()

        # Check traces
        def pop_trace(name: str, filter: Callable[[Any], bool] | None = None) -> Any:
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
        # One initial, one child, one external, one continue as new
        assert pop_trace("workflow.execute", lambda v: v.args[0] == "initial")
        assert pop_trace("workflow.execute", lambda v: v.args[0] == "child")
        assert pop_trace("workflow.execute", lambda v: v.args[0] == "external")
        assert pop_trace("workflow.execute", lambda v: v.args[0] == "continue-as-new")
        assert pop_trace("workflow.signal", lambda v: v.args[0] == "signal-val")
        assert pop_trace("workflow.query", lambda v: v.args[0] == "query-val")
        assert pop_trace("workflow.continue_as_new")
        assert pop_trace("workflow.info")
        assert pop_trace("workflow.start_activity", lambda v: v.args[0] == "val1")
        assert pop_trace("workflow.start_local_activity", lambda v: v.args[0] == "val2")
        assert pop_trace(
            "workflow.start_child_workflow", lambda v: v.args[0] == "child"
        )
        assert pop_trace(
            "workflow.signal_child_workflow", lambda v: v.args[0] == "child-signal-val"
        )
        assert pop_trace("workflow.signal", lambda v: v.args[0] == "child-signal-val")
        assert pop_trace(
            "workflow.start_child_workflow", lambda v: v.args[0] == "external"
        )
        assert pop_trace(
            "workflow.signal_external_workflow",
            lambda v: v.args[0] == "external-signal-val",
        )
        assert pop_trace("workflow.info")
        assert pop_trace("workflow.start_nexus_operation")
        assert pop_trace(
            "workflow.signal", lambda v: v.args[0] == "external-signal-val"
        )
        assert pop_trace("workflow.update.handler", lambda v: v.args[0] == "update-val")
        assert pop_trace(
            "workflow.update.validator", lambda v: v.args[0] == "reject-me"
        )
        assert pop_trace(
            "nexus.start_operation.InterceptedNexusService.intercepted_operation",
            lambda v: v.input == "nexus-workflow",
        )
        assert pop_trace("workflow.execute", lambda v: v.args[0] == "nexus-workflow")
        assert pop_trace(
            "nexus.cancel_operation.InterceptedNexusService.intercepted_operation",
        )

        # Confirm no unexpected traces
        assert not interceptor_traces


class WorkflowInstanceAccessInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return WorkflowInstanceAccessInboundInterceptor


class WorkflowInstanceAccessInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> int:
        # Return integer difference between ids of workflow instance obtained from workflow run method and
        # from workflow.instance(). They should be the same, so the difference should be 0.
        from_workflow_instance_api = workflow.instance()
        assert from_workflow_instance_api is not None
        id_from_workflow_instance_api = id(from_workflow_instance_api)
        id_from_workflow_run_method = await super().execute_workflow(input)
        return id_from_workflow_run_method - id_from_workflow_instance_api


@workflow.defn
class WorkflowInstanceAccessWorkflow:
    @workflow.run
    async def run(self) -> int:
        return id(self)


async def test_workflow_instance_access_from_interceptor(client: Client):
    task_queue = f"task_queue_{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[WorkflowInstanceAccessWorkflow],
        interceptors=[WorkflowInstanceAccessInterceptor()],
    ):
        difference = await client.execute_workflow(
            WorkflowInstanceAccessWorkflow.run,
            id=f"workflow_{uuid.uuid4()}",
            task_queue=task_queue,
        )
        assert difference == 0
