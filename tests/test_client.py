import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Tuple

import pytest
from google.protobuf import json_format

import temporalio.api.enums.v1
import temporalio.common
import temporalio.exceptions
from temporalio import workflow
from temporalio.api.enums.v1 import (
    CancelExternalWorkflowExecutionFailedCause,
    ContinueAsNewInitiator,
    EventType,
    ParentClosePolicy,
    RetryState,
    SignalExternalWorkflowExecutionFailedCause,
    StartChildWorkflowExecutionFailedCause,
    TaskQueueKind,
    TimeoutType,
    WorkflowIdReusePolicy,
    WorkflowTaskFailedCause,
)
from temporalio.api.history.v1 import History
from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
from temporalio.client import (
    CancelWorkflowInput,
    Client,
    Interceptor,
    OutboundInterceptor,
    QueryWorkflowInput,
    RPCError,
    RPCStatusCode,
    SignalWorkflowInput,
    StartWorkflowInput,
    TerminateWorkflowInput,
    WorkflowContinuedAsNewError,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryRejectedError,
    _history_from_json,
)
from temporalio.testing import WorkflowEnvironment
from tests.helpers import new_worker
from tests.helpers.worker import (
    ExternalWorker,
    KSAction,
    KSContinueAsNewAction,
    KSErrorAction,
    KSQueryHandlerAction,
    KSResultAction,
    KSSignalAction,
    KSSleepAction,
    KSWorkflowParams,
)


async def test_start_id_reuse(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1220"
        )
    # Run to return "some result"
    id = str(uuid.uuid4())
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result"))]
        ),
        id=id,
        task_queue=worker.task_queue,
    )
    assert "some result" == await handle.result()
    # Run again with reject duplicate
    with pytest.raises(RPCError) as err:
        handle = await client.start_workflow(
            "kitchen_sink",
            KSWorkflowParams(
                actions=[KSAction(result=KSResultAction(value="some result 2"))]
            ),
            id=id,
            task_queue=worker.task_queue,
            id_reuse_policy=temporalio.common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        await handle.result()
    assert err.value.status == RPCStatusCode.ALREADY_EXISTS

    # Run again allowing duplicate (the default)
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result 3"))]
        ),
        id=id,
        task_queue=worker.task_queue,
    )
    assert "some result 3" == await handle.result()


async def test_start_with_signal(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(action_signal="my-signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        start_signal="my-signal",
        start_signal_args=[KSAction(result=KSResultAction(value="some signal arg"))],
    )
    assert "some signal arg" == await handle.result()


async def test_result_follow_continue_as_new(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(continue_as_new=KSContinueAsNewAction(while_above_zero=1)),
                KSAction(result=KSResultAction(run_id=True)),
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    final_run_id = await handle.result()
    assert len(final_run_id) > 5 and handle.run_id != final_run_id

    # Get a handle and check result without following and confirm
    # continue-as-new error
    with pytest.raises(WorkflowContinuedAsNewError) as err:
        await handle.result(follow_runs=False)
    assert err.value.new_execution_run_id == final_run_id


async def test_workflow_failed(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(
                    error=KSErrorAction(
                        message="some error", details={"foo": "bar", "baz": 123.45}
                    )
                )
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.ApplicationError)
    assert str(err.value.cause) == "some error"
    assert list(err.value.cause.details)[0] == {"foo": "bar", "baz": 123.45}


async def test_cancel(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(sleep=KSSleepAction(millis=50000))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.cancel()
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.CancelledError)


async def test_terminate(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(sleep=KSSleepAction(millis=50000))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.terminate("arg1", "arg2", reason="some reason")
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.TerminatedError)
    assert str(err.value.cause) == "some reason"
    assert list(err.value.cause.details) == ["arg1", "arg2"]


async def test_cancel_not_found(client: Client):
    with pytest.raises(RPCError) as err:
        await client.get_workflow_handle("does-not-exist").cancel()
    assert err.value.status == RPCStatusCode.NOT_FOUND


async def test_describe(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1425"
        )
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(result=KSResultAction(value="some value"))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        memo={"foo": "bar"},
    )
    assert "some value" == await handle.result()
    desc = await handle.describe()
    assert desc.close_time and abs(
        desc.close_time - datetime.now(timezone.utc)
    ) < timedelta(seconds=20)
    assert desc.execution_time and abs(
        desc.execution_time - datetime.now(timezone.utc)
    ) < timedelta(seconds=20)
    assert desc.id == handle.id
    assert (await desc.memo()) == {"foo": "bar"}
    assert not desc.parent_id
    assert not desc.parent_run_id
    assert desc.run_id == handle.first_execution_run_id
    assert abs(desc.start_time - datetime.now(timezone.utc)) < timedelta(seconds=20)
    assert desc.status == WorkflowExecutionStatus.COMPLETED
    assert desc.task_queue == worker.task_queue
    assert desc.workflow_type == "kitchen_sink"


async def test_query(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(query_handler=KSQueryHandlerAction(name="some query"))]
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.result()
    assert "some query arg" == await handle.query("some query", "some query arg")
    # Try a query not on the workflow
    with pytest.raises(RPCError) as err:
        await handle.query("does not exist")
    # TODO(cretz): Is this the status we expect all SDKs to report?
    assert err.value.status == RPCStatusCode.INVALID_ARGUMENT


async def test_query_rejected(client: Client, worker: ExternalWorker):
    # Make a queryable workflow that waits on a signal
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(query_handler=KSQueryHandlerAction(name="some query")),
                KSAction(signal=KSSignalAction(name="some signal")),
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    # Confirm we can query w/ a not-open rejection condition since it's still
    # open
    assert "some query arg" == await handle.query(
        "some query",
        "some query arg",
        reject_condition=temporalio.common.QueryRejectCondition.NOT_OPEN,
    )
    # But if we signal then wait for result, that same query should fail
    await handle.signal("some signal", "some signal arg")
    await handle.result()
    with pytest.raises(WorkflowQueryRejectedError) as err:
        assert "some query arg" == await handle.query(
            "some query",
            "some query arg",
            reject_condition=temporalio.common.QueryRejectCondition.NOT_OPEN,
        )
    assert err.value.status == WorkflowExecutionStatus.COMPLETED


async def test_signal(client: Client, worker: ExternalWorker):
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(action_signal="some signal"),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.signal(
        "some signal",
        KSAction(result=KSResultAction(value="some signal arg")),
    )
    assert "some signal arg" == await handle.result()


async def test_retry_policy(client: Client, worker: ExternalWorker):
    # Make the workflow retry 3 times w/ no real backoff
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(actions=[KSAction(error=KSErrorAction(attempt=True))]),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
        retry_policy=temporalio.common.RetryPolicy(
            initial_interval=timedelta(milliseconds=1),
            maximum_attempts=3,
        ),
    )
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, temporalio.exceptions.ApplicationError)
    assert str(err.value.cause) == "attempt 3"


async def test_single_client_config_change(client: Client, worker: ExternalWorker):
    # Make sure normal query works on completed workflow
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(query_handler=KSQueryHandlerAction(name="some query"))]
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.result()
    assert "some query arg" == await handle.query("some query", "some query arg")
    # Now create a client with the rejection condition changed to not open
    config = client.config()
    config[
        "default_workflow_query_reject_condition"
    ] = temporalio.common.QueryRejectCondition.NOT_OPEN
    reject_client = Client(**config)
    with pytest.raises(WorkflowQueryRejectedError):
        await reject_client.get_workflow_handle(handle.id).query(
            "some query", "some query arg"
        )


class TracingClientInterceptor(Interceptor):
    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        self.traces: List[Tuple[str, Any]] = []
        return TracingClientOutboundInterceptor(self, next)


class TracingClientOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self,
        parent: TracingClientInterceptor,
        next: OutboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def start_workflow(
        self, input: StartWorkflowInput
    ) -> WorkflowHandle[Any, Any]:
        self._parent.traces.append(("start_workflow", input))
        return await super().start_workflow(input)

    async def cancel_workflow(self, input: CancelWorkflowInput) -> None:
        self._parent.traces.append(("cancel_workflow", input))
        return await super().cancel_workflow(input)

    async def query_workflow(self, input: QueryWorkflowInput) -> Any:
        self._parent.traces.append(("query_workflow", input))
        return await super().query_workflow(input)

    async def signal_workflow(self, input: SignalWorkflowInput) -> None:
        self._parent.traces.append(("signal_workflow", input))
        return await super().signal_workflow(input)

    async def terminate_workflow(self, input: TerminateWorkflowInput) -> None:
        self._parent.traces.append(("terminate_workflow", input))
        return await super().terminate_workflow(input)


async def test_interceptor(client: Client, worker: ExternalWorker):
    # Create new client from existing client but with a tracing interceptor
    interceptor = TracingClientInterceptor()
    config = client.config()
    config["interceptors"] = [interceptor]
    client = Client(**config)
    # Do things that would trigger the interceptors
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(query_handler=KSQueryHandlerAction(name="some query")),
                KSAction(signal=KSSignalAction(name="some signal")),
            ],
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    await handle.query("some query", "some query arg")
    await handle.signal("some signal")
    await handle.result()
    await handle.cancel()
    # Ignore this error
    with pytest.raises(RPCError):
        await handle.terminate()

    # Check trace
    assert len(interceptor.traces) == 5
    assert interceptor.traces[0][0] == "start_workflow"
    assert interceptor.traces[0][1].workflow == "kitchen_sink"
    assert interceptor.traces[1][0] == "query_workflow"
    assert interceptor.traces[1][1].query == "some query"
    assert interceptor.traces[2][0] == "signal_workflow"
    assert interceptor.traces[2][1].signal == "some signal"
    assert interceptor.traces[3][0] == "cancel_workflow"
    assert interceptor.traces[3][1].id == handle.id
    assert interceptor.traces[4][0] == "terminate_workflow"
    assert interceptor.traces[4][1].id == handle.id


async def test_lazy_client(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1094"
        )
    # Create another client that is lazy. This test just makes sure the
    # functionality continues to work.
    lazy_client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        lazy=True,
    )
    assert not lazy_client.service_client.worker_service_client._bridge_client
    await lazy_client.workflow_service.get_system_info(GetSystemInfoRequest())
    assert lazy_client.service_client.worker_service_client._bridge_client


@workflow.defn
class ListableWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_list_workflows_and_fetch_history(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support newer workflow listing")

    # Run 5 workflows. Use the same workflow ID over and over to make sure we
    # don't clash with other tests
    workflow_id = f"workflow-{uuid.uuid4()}"
    expected_id_and_input = []
    async with new_worker(client, ListableWorkflow) as worker:
        for i in range(5):
            await client.execute_workflow(
                ListableWorkflow.run,
                f"user{i}",
                id=workflow_id,
                task_queue=worker.task_queue,
            )
            expected_id_and_input.append((workflow_id, f'"user{i}"'))

    # List them and get their history
    actual_id_and_input = sorted(
        [
            (
                hist.workflow_id,
                hist.events[0]
                .workflow_execution_started_event_attributes.input.payloads[0]
                .data.decode(),
            )
            async for hist in client.list_workflows(
                f"WorkflowId = '{workflow_id}'"
            ).map_histories()
        ]
    )
    assert actual_id_and_input == expected_id_and_input


def test_history_from_json():
    # Take proto, make JSON, convert to dict, alter some enums, confirm that it
    # alters the enums back and matches original history

    # Make history with some enums, one one each event
    history = History()
    history.events.add().request_cancel_external_workflow_execution_failed_event_attributes.cause = (
        CancelExternalWorkflowExecutionFailedCause.CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    )
    history.events.add().workflow_execution_started_event_attributes.initiator = (
        ContinueAsNewInitiator.CONTINUE_AS_NEW_INITIATOR_CRON_SCHEDULE
    )
    history.events.add().event_type = (
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.parent_close_policy = (
        ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    )
    history.events.add().workflow_execution_failed_event_attributes.retry_state = (
        RetryState.RETRY_STATE_CANCEL_REQUESTED
    )
    history.events.add().signal_external_workflow_execution_failed_event_attributes.cause = (
        SignalExternalWorkflowExecutionFailedCause.SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    )
    history.events.add().start_child_workflow_execution_failed_event_attributes.cause = (
        StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND
    )
    history.events.add().workflow_execution_started_event_attributes.task_queue.kind = (
        TaskQueueKind.TASK_QUEUE_KIND_NORMAL
    )
    history.events.add().workflow_task_timed_out_event_attributes.timeout_type = (
        TimeoutType.TIMEOUT_TYPE_HEARTBEAT
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.workflow_id_reuse_policy = (
        WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )
    history.events.add().workflow_task_failed_event_attributes.cause = (
        WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_BAD_BINARY
    )
    history.events.add().workflow_execution_started_event_attributes.continued_failure.timeout_failure_info.timeout_type = (
        TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
    )
    history.events.add().activity_task_started_event_attributes.last_failure.activity_failure_info.retry_state = (
        RetryState.RETRY_STATE_IN_PROGRESS
    )
    history.events.add().workflow_execution_failed_event_attributes.failure.cause.child_workflow_execution_failure_info.retry_state = (
        RetryState.RETRY_STATE_INTERNAL_SERVER_ERROR
    )

    # Convert to JSON dict and alter enums to pascal versions
    bad_history_dict = json_format.MessageToDict(history)
    e = bad_history_dict["events"]
    e[0]["requestCancelExternalWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "ExternalWorkflowExecutionNotFound"
    e[1]["workflowExecutionStartedEventAttributes"]["initiator"] = "CronSchedule"
    e[2]["eventType"] = "ActivityTaskCancelRequested"
    e[3]["startChildWorkflowExecutionInitiatedEventAttributes"][
        "parentClosePolicy"
    ] = "Abandon"
    e[4]["workflowExecutionFailedEventAttributes"]["retryState"] = "CancelRequested"
    e[5]["signalExternalWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "ExternalWorkflowExecutionNotFound"
    e[6]["startChildWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "NamespaceNotFound"
    e[7]["workflowExecutionStartedEventAttributes"]["taskQueue"]["kind"] = "Normal"
    e[8]["workflowTaskTimedOutEventAttributes"]["timeoutType"] = "Heartbeat"
    e[9]["startChildWorkflowExecutionInitiatedEventAttributes"][
        "workflowIdReusePolicy"
    ] = "AllowDuplicate"
    e[10]["workflowTaskFailedEventAttributes"]["cause"] = "BadBinary"
    e[11]["workflowExecutionStartedEventAttributes"]["continuedFailure"][
        "timeoutFailureInfo"
    ]["timeoutType"] = "ScheduleToClose"
    e[12]["activityTaskStartedEventAttributes"]["lastFailure"]["activityFailureInfo"][
        "retryState"
    ] = "InProgress"
    e[13]["workflowExecutionFailedEventAttributes"]["failure"]["cause"][
        "childWorkflowExecutionFailureInfo"
    ]["retryState"] = "InternalServerError"

    # Apply fixes
    history_from_dict = _history_from_json(bad_history_dict)
    history_from_json = _history_from_json(json.dumps(bad_history_dict))

    # Check
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        history_from_dict
    )
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        history_from_json
    )

    # Confirm double-encode does not cause issues
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        _history_from_json(json_format.MessageToDict(history_from_dict))
    )
