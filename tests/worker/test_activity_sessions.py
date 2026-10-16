"""Serverless tests of per-activity session routing commands."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import temporalio.activity as activity
import temporalio.api.common.v1
import temporalio.bridge.proto.workflow_activation as workflow_activation
import temporalio.common as common
import temporalio.converter as converter
import temporalio.worker as worker
import temporalio.workflow as workflow


@activity.defn
async def session_activity() -> None:
    pass


@activity.defn
class SessionActivities:
    async def __call__(self) -> None:
        pass

    @activity.defn
    async def method(self) -> None:
        pass


@workflow.defn
class ActivitySessionWorkflow:
    @workflow.run
    async def run(self, api: str, sticky: bool | None, task_queue: str | None) -> None:
        config: workflow.ActivityConfig = {
            "start_to_close_timeout": timedelta(seconds=10),
            "task_queue": task_queue,
        }
        if sticky is not None:
            config["session_options"] = workflow.ActivitySessionOptions(
                workflow_sticky_session=sticky
            )
        if api == "start":
            await workflow.start_activity(session_activity, **config)
        elif api == "execute":
            await workflow.execute_activity(session_activity, **config)
        elif api == "start-name":
            await workflow.start_activity("session_activity", **config)
        elif api == "execute-name":
            await workflow.execute_activity("session_activity", **config)
        elif api == "start-class":
            await workflow.start_activity_class(SessionActivities, **config)
        elif api == "execute-class":
            await workflow.execute_activity_class(SessionActivities, **config)
        elif api == "start-method":
            await workflow.start_activity_method(SessionActivities.method, **config)
        elif api == "execute-method":
            await workflow.execute_activity_method(SessionActivities.method, **config)
        else:
            raise AssertionError(f"Unknown activity API: {api}")


@pytest.mark.parametrize(
    "api",
    [
        "start",
        "execute",
        "start-name",
        "execute-name",
        "start-class",
        "execute-class",
        "start-method",
        "execute-method",
    ],
)
@pytest.mark.parametrize("sticky", [None, False, True])
@pytest.mark.parametrize(
    "task_queue,disable_eager",
    [(None, False), ("workflow-queue", True), ("other-queue", False)],
)
@pytest.mark.parametrize("replace_options", [False, True])
async def test_activity_session_commands(
    api: str,
    sticky: bool | None,
    task_queue: str | None,
    disable_eager: bool,
    replace_options: bool,
) -> None:
    seen: list[worker.StartActivityInput] = []

    class Outbound(worker.WorkflowOutboundInterceptor):
        def start_activity(
            self, input: worker.StartActivityInput
        ) -> workflow.ActivityHandle[Any]:
            assert input.task_queue == task_queue
            assert input.disable_eager_execution == disable_eager
            if sticky is None:
                assert input.session_options is None
            else:
                assert input.session_options == workflow.ActivitySessionOptions(sticky)
            if replace_options:
                input.session_options = workflow.ActivitySessionOptions(not sticky)
            seen.append(input)
            return super().start_activity(input)

    class Inbound(worker.WorkflowInboundInterceptor):
        def init(self, outbound: worker.WorkflowOutboundInterceptor) -> None:
            super().init(Outbound(outbound))

    start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    instance = worker.UnsandboxedWorkflowRunner().create_instance(
        worker.WorkflowInstanceDetails(
            payload_converter_factory=converter.DefaultPayloadConverter,
            failure_converter_class=converter.DefaultFailureConverter,
            interceptor_classes=[Inbound],
            defn=workflow._Definition.must_from_class(ActivitySessionWorkflow),
            info=workflow.Info(
                attempt=1,
                continued_run_id=None,
                cron_schedule=None,
                execution_timeout=None,
                first_execution_run_id="session-run",
                headers={},
                namespace="test-namespace",
                original_execution_run_id="session-run",
                parent=None,
                root=None,
                priority=common.Priority(),
                raw_memo={},
                retry_policy=None,
                run_id="session-run",
                run_timeout=None,
                search_attributes={},
                start_time=start_time,
                task_queue="workflow-queue",
                task_timeout=timedelta(seconds=10),
                typed_search_attributes=common.TypedSearchAttributes.empty,
                workflow_id="session-id",
                workflow_start_time=start_time,
                workflow_type="ActivitySessionWorkflow",
            ),
            randomness_seed=1,
            extern_functions={},
            disable_eager_activity_execution=disable_eager,
            worker_level_failure_exception_types=[],
            patch_activation_callback=None,
            last_completion_result=temporalio.api.common.v1.Payloads(),
            last_failure=None,
        )
    )
    try:
        completion = await asyncio.to_thread(
            instance.activate,
            workflow_activation.WorkflowActivation(
                run_id="session-run",
                jobs=[
                    workflow_activation.WorkflowActivationJob(
                        initialize_workflow=workflow_activation.InitializeWorkflow(
                            workflow_type="ActivitySessionWorkflow",
                            workflow_id="session-id",
                            randomness_seed=1,
                            arguments=converter.DefaultPayloadConverter().to_payloads(
                                [api, sticky, task_queue]
                            ),
                        )
                    )
                ],
            ),
        )
        assert completion.HasField("successful"), completion
        assert len(seen) == 1
        assert len(completion.successful.commands) == 1
        command = completion.successful.commands[0]
        assert command.HasField("schedule_activity")
        schedule = command.schedule_activity
        assert schedule.task_queue == (task_queue or "workflow-queue")
        assert schedule.do_not_eagerly_execute == disable_eager
        expected_sticky = not sticky if replace_options else sticky
        if expected_sticky is None:
            assert not schedule.HasField("session_options")
        else:
            assert schedule.HasField("session_options")
            assert schedule.session_options.HasField("workflow_sticky_session")
            assert schedule.session_options.workflow_sticky_session == expected_sticky
    finally:
        await asyncio.to_thread(
            instance.activate,
            workflow_activation.WorkflowActivation(
                run_id="session-run",
                jobs=[
                    workflow_activation.WorkflowActivationJob(
                        remove_from_cache=workflow_activation.RemoveFromCache()
                    )
                ],
            ),
        )
