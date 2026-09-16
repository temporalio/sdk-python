from __future__ import annotations

import concurrent.futures
import uuid
from datetime import timedelta

import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.activity as activity
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.nexus
import temporalio.testing
import temporalio.worker
import temporalio.workflow as workflow
from tests.helpers import new_worker
from tests.helpers.nexus import make_nexus_endpoint_name


class TransferValueConverter(
    temporalio.converter.TransferTypeConverter["TransferValue", str]
):
    transfer_type = str

    def to_transfer_type(self, value: TransferValue) -> str:
        return f"transfer:{value.text}"

    def from_transfer_type(
        self, value: str, type_hint: type[TransferValue]
    ) -> TransferValue:
        assert type_hint is TransferValue
        assert value.startswith("transfer:")
        return TransferValue(value.removeprefix("transfer:"))


@temporalio.converter.transfer_type_convertible(TransferValueConverter)
class TransferValue:
    # A non-dataclass ensures missing transfer conversion cannot silently fall
    # back to the default converter's dataclass JSON support.
    def __init__(self, text: str) -> None:
        self.text = text

    def append(self, step: str) -> TransferValue:
        return TransferValue(f"{self.text}/{step}")


def test_transfer_value_requires_transfer_conversion():
    with pytest.raises(TypeError, match="not JSON serializable"):
        temporalio.converter.DefaultPayloadConverter().to_payload(
            TransferValue("input")
        )


@workflow.defn
class TransferWorkflow:
    @workflow.run
    async def run(self, value: TransferValue, operation: str) -> TransferValue:
        if operation == "child":
            result = await workflow.execute_child_workflow(
                TransferWorkflow.run,
                args=[value.append("child-input"), "return"],
                id=f"{workflow.info().workflow_id}-child",
            )
            return result.append("parent")
        if operation == "continue-as-new":
            workflow.continue_as_new(args=[value.append("continued"), "return"])
        assert operation == "return"
        return value.append("workflow")


@pytest.mark.parametrize(
    "operation, expected",
    [
        ("return", "input/workflow"),
        ("child", "input/child-input/workflow/parent"),
        ("continue-as-new", "input/continued/workflow"),
    ],
)
async def test_transfer_types_workflow(
    client: temporalio.client.Client, operation: str, expected: str
):
    async with new_worker(
        client, TransferWorkflow, workflow_failure_exception_types=[Exception]
    ) as worker:
        result = await client.execute_workflow(
            TransferWorkflow.run,
            args=[TransferValue("input"), operation],
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert result.text == expected


@activity.defn
async def transfer_activity(value: TransferValue) -> TransferValue:
    return value.append("activity")


@activity.defn
def transfer_sync_activity(value: TransferValue) -> TransferValue:
    return value.append("activity")


@workflow.defn
class TransferActivityWorkflow:
    @workflow.run
    async def run(
        self, value: TransferValue, local: bool, synchronous: bool
    ) -> TransferValue:
        activity_fn = transfer_sync_activity if synchronous else transfer_activity
        if local:
            result = await workflow.execute_local_activity(
                activity_fn,
                value.append("activity-input"),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
            )
        else:
            result = await workflow.execute_activity(
                activity_fn,
                value.append("activity-input"),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
            )
        assert isinstance(result, TransferValue)
        return result.append("workflow")


@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
@pytest.mark.parametrize("execution", ["async", "thread", "process"])
async def test_transfer_types_activity(
    client: temporalio.client.Client,
    shared_state_manager: temporalio.worker.SharedStateManager,
    local: bool,
    execution: str,
):
    executor_cls = (
        concurrent.futures.ProcessPoolExecutor
        if execution == "process"
        else concurrent.futures.ThreadPoolExecutor
    )
    with executor_cls(max_workers=2) as executor:
        async with new_worker(
            client,
            TransferActivityWorkflow,
            activities=[transfer_activity, transfer_sync_activity],
            activity_executor=executor,
            shared_state_manager=shared_state_manager,
            workflow_failure_exception_types=[Exception],
        ) as worker:
            result = await client.execute_workflow(
                TransferActivityWorkflow.run,
                args=[TransferValue("input"), local, execution != "async"],
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            assert result.text == "input/activity-input/activity/workflow"


@workflow.defn
class TransferMessagesWorkflow:
    def __init__(self) -> None:
        self.value = TransferValue("unset")
        self.finished = False

    @workflow.run
    async def run(self, value: TransferValue) -> TransferValue:
        await workflow.wait_condition(lambda: self.finished)
        return self.value.append(value.text)

    @workflow.signal
    def signal(self, value: TransferValue) -> None:
        self.value = value.append("signal")

    @workflow.query
    def query(self, value: TransferValue) -> TransferValue:
        return self.value.append(value.text)

    @workflow.update
    async def update(self, value: TransferValue) -> TransferValue:
        self.value = value.append("update")
        return self.value.append("result")

    @update.validator
    def validate_update(self, value: TransferValue) -> None:
        assert value.text

    @workflow.signal
    def finish(self) -> None:
        self.finished = True


@pytest.mark.parametrize(
    "start", ["workflow", "signal-with-start", "update-with-start"]
)
async def test_transfer_types_messages(client: temporalio.client.Client, start: str):
    async with new_worker(
        client, TransferMessagesWorkflow, workflow_failure_exception_types=[Exception]
    ) as worker:
        if start == "update-with-start":
            start_op = temporalio.client.WithStartWorkflowOperation(
                TransferMessagesWorkflow.run,
                TransferValue("workflow-input"),
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
                id_conflict_policy=temporalio.common.WorkflowIDConflictPolicy.FAIL,
            )
            result = await client.execute_update_with_start_workflow(
                TransferMessagesWorkflow.update,
                TransferValue("start-update-input"),
                start_workflow_operation=start_op,
            )
            assert result.text == "start-update-input/update/result"
            handle = await start_op.workflow_handle()
        else:
            handle = await client.start_workflow(
                TransferMessagesWorkflow.run,
                TransferValue("workflow-input"),
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
                start_signal="signal" if start == "signal-with-start" else None,
                start_signal_args=[TransferValue("signal-input")]
                if start == "signal-with-start"
                else [],
            )
        if start != "signal-with-start":
            await handle.signal(
                TransferMessagesWorkflow.signal, TransferValue("signal-input")
            )
        result = await handle.query(
            TransferMessagesWorkflow.query, TransferValue("query-input")
        )
        assert result.text == "signal-input/signal/query-input"

        result = await handle.execute_update(
            TransferMessagesWorkflow.update, TransferValue("update-input")
        )
        assert result.text == "update-input/update/result"
        await handle.signal(TransferMessagesWorkflow.finish)
        assert (await handle.result()).text == "update-input/update/workflow-input"


@workflow.defn
class TransferSignalWorkflow:
    @workflow.run
    async def run(self, value: TransferValue, external: bool) -> TransferValue:
        child = await workflow.start_child_workflow(
            TransferMessagesWorkflow.run,
            value.append("child-input"),
            id=f"{workflow.info().workflow_id}-child",
        )
        if external:
            handle = workflow.get_external_workflow_handle_for(
                TransferMessagesWorkflow.run, child.id
            )
            await handle.signal(
                TransferMessagesWorkflow.signal, value.append("external")
            )
            await handle.signal(TransferMessagesWorkflow.finish)
        else:
            await child.signal(TransferMessagesWorkflow.signal, value.append("child"))
            await child.signal(TransferMessagesWorkflow.finish)
        return (await child).append("parent")


@pytest.mark.parametrize("external", [False, True], ids=["child", "external"])
async def test_transfer_types_workflow_signal(
    client: temporalio.client.Client, external: bool
):
    async with new_worker(
        client,
        TransferSignalWorkflow,
        TransferMessagesWorkflow,
        workflow_failure_exception_types=[Exception],
    ) as worker:
        result = await client.execute_workflow(
            TransferSignalWorkflow.run,
            args=[TransferValue("input"), external],
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        signal_target = "external" if external else "child"
        assert result.text == f"input/{signal_target}/signal/input/child-input/parent"


@activity.defn
async def transfer_failure_activity(value: TransferValue) -> None:
    raise temporalio.exceptions.ApplicationError("failure", value.append("activity"))


@workflow.defn
class TransferFailureWorkflow:
    @workflow.run
    async def run(self, value: TransferValue, operation: str) -> None:
        if operation == "workflow":
            raise temporalio.exceptions.ApplicationError(
                "failure", value.append("workflow")
            )
        try:
            if operation == "local-activity":
                await workflow.execute_local_activity(
                    transfer_failure_activity,
                    value,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
                )
            else:
                await workflow.execute_activity(
                    transfer_failure_activity,
                    value,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
                )
        except temporalio.exceptions.FailureError as err:
            cause = (
                err.cause
                if isinstance(err, temporalio.exceptions.ActivityError)
                else err
            )
            assert isinstance(cause, temporalio.exceptions.ApplicationError)
            # Failure details have no type hints, so decoding yields the transfer type.
            assert cause.details == ("transfer:input/activity",)
            raise


@pytest.mark.parametrize("operation", ["workflow", "activity", "local-activity"])
async def test_transfer_types_failure_details(
    client: temporalio.client.Client, operation: str
):
    async with new_worker(
        client,
        TransferFailureWorkflow,
        activities=[transfer_failure_activity],
        workflow_failure_exception_types=[Exception],
    ) as worker:
        with pytest.raises(temporalio.client.WorkflowFailureError) as err:
            await client.execute_workflow(
                TransferFailureWorkflow.run,
                args=[TransferValue("input"), operation],
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
        cause: BaseException | None = err.value.cause
        if isinstance(cause, temporalio.exceptions.ActivityError):
            cause = cause.cause
        assert isinstance(cause, temporalio.exceptions.ApplicationError)
        source = "workflow" if operation == "workflow" else "activity"
        assert cause.details == (f"transfer:input/{source}",)


@nexusrpc.service
class TransferService:
    sync: nexusrpc.Operation[TransferValue, TransferValue]
    async_: nexusrpc.Operation[TransferValue, TransferValue]


@nexusrpc.handler.service_handler(service=TransferService)
class TransferServiceHandler:
    @nexusrpc.handler.sync_operation
    async def sync(
        self, _ctx: nexusrpc.handler.StartOperationContext, input: TransferValue
    ) -> TransferValue:
        return input.append("nexus-sync")

    @temporalio.nexus.workflow_run_operation
    async def async_(
        self, ctx: temporalio.nexus.WorkflowRunOperationContext, input: TransferValue
    ) -> temporalio.nexus.WorkflowHandle[TransferValue]:
        return await ctx.start_workflow(
            TransferWorkflow.run,
            args=[input.append("nexus-async"), "return"],
            id=str(uuid.uuid4()),
        )


@workflow.defn
class TransferNexusWorkflow:
    @workflow.run
    async def run(
        self, value: TransferValue, endpoint: str, synchronous: bool
    ) -> TransferValue:
        client = workflow.create_nexus_client(
            service=TransferService, endpoint=endpoint
        )
        result = await client.execute_operation(
            TransferService.sync if synchronous else TransferService.async_,
            value.append("nexus-input"),
            schedule_to_close_timeout=timedelta(seconds=20),
        )
        return result.append("caller")


# Cloud CI credentials cannot manage Nexus endpoints.
@pytest.mark.requires_local_server
@pytest.mark.parametrize("synchronous", [False, True], ids=["async", "sync"])
async def test_transfer_types_nexus(
    env: temporalio.testing.WorkflowEnvironment, synchronous: bool
):
    if env.supports_time_skipping:
        pytest.skip("Nexus requires the dev server")
    task_queue = str(uuid.uuid4())
    endpoint = await env.create_nexus_endpoint(
        make_nexus_endpoint_name(task_queue), task_queue
    )
    try:
        async with new_worker(
            env.client,
            TransferNexusWorkflow,
            TransferWorkflow,
            task_queue=task_queue,
            nexus_service_handlers=[TransferServiceHandler()],
            workflow_failure_exception_types=[Exception],
        ):
            result = await env.client.execute_workflow(
                TransferNexusWorkflow.run,
                args=[TransferValue("input"), endpoint.spec.name, synchronous],
                id=str(uuid.uuid4()),
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            expected = "nexus-sync" if synchronous else "nexus-async/workflow"
            assert result.text == f"input/nexus-input/{expected}/caller"
    finally:
        await env.delete_nexus_endpoint(endpoint)
