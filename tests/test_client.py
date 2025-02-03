import dataclasses
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Tuple, cast
from unittest import mock

import google.protobuf.any_pb2
import google.protobuf.message
import pytest
from google.protobuf import json_format

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.errordetails.v1
import temporalio.api.workflowservice.v1
import temporalio.common
import temporalio.exceptions
from temporalio import workflow
from temporalio.api.cloud.cloudservice.v1 import GetNamespaceRequest
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
    BuildIdOpAddNewCompatible,
    BuildIdOpAddNewDefault,
    BuildIdOpMergeSets,
    BuildIdOpPromoteBuildIdWithinSet,
    BuildIdOpPromoteSetByBuildId,
    CancelWorkflowInput,
    Client,
    CloudOperationsClient,
    Interceptor,
    OutboundInterceptor,
    QueryWorkflowInput,
    RPCError,
    RPCStatusCode,
    Schedule,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleBackfill,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
    SignalWorkflowInput,
    StartWorkflowInput,
    StartWorkflowUpdateInput,
    TaskReachabilityType,
    TerminateWorkflowInput,
    WorkflowContinuedAsNewError,
    WorkflowExecutionCount,
    WorkflowExecutionCountAggregationGroup,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
    WorkflowUpdateHandle,
    _history_from_json,
)
from temporalio.common import (
    RetryPolicy,
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)
from temporalio.converter import DataConverter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import ServiceCall
from temporalio.testing import WorkflowEnvironment
from tests.helpers import (
    assert_eq_eventually,
    ensure_search_attributes_present,
    new_worker,
    worker_versioning_enabled,
)
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
    with pytest.raises(WorkflowAlreadyStartedError) as err:
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
    assert err.value.run_id == handle.result_run_id

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


async def test_start_delay(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support start delay")
    start_delay = timedelta(hours=1, minutes=20, seconds=30)
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result"))]
        ),
        id=f"workflow-{uuid.uuid4()}",
        task_queue=worker.task_queue,
        start_delay=start_delay,
    )
    # Check that first event has start delay
    first_event = [e async for e in handle.fetch_history_events()][0]
    assert (
        start_delay
        == first_event.workflow_execution_started_event_attributes.first_workflow_task_backoff.ToTimedelta()
    )


async def test_signal_with_start_delay(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support start delay")
    start_delay = timedelta(hours=1, minutes=20, seconds=30)
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[KSAction(result=KSResultAction(value="some result"))]
        ),
        id=f"workflow-{uuid.uuid4()}",
        task_queue=worker.task_queue,
        start_delay=start_delay,
        start_signal="some-signal",
    )
    # Check that first event has start delay
    first_event = [e async for e in handle.fetch_history_events()][0]
    assert (
        start_delay
        == first_event.workflow_execution_started_event_attributes.first_workflow_task_backoff.ToTimedelta()
    )


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


async def test_rpc_already_exists_error_is_raised(client: Client):
    class start_workflow_execution(
        ServiceCall[
            temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest,
            temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse,
        ]
    ):
        already_exists_err = RPCError(
            "fake already exists error", RPCStatusCode.ALREADY_EXISTS, b""
        )
        already_exists_err._grpc_status = temporalio.api.common.v1.GrpcStatus(
            details=[
                google.protobuf.any_pb2.Any(
                    type_url="not-WorkflowExecutionAlreadyStartedFailure", value=b""
                )
            ],
        )

        def __init__(self) -> None:
            pass

        async def __call__(
            self,
            req: temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest,
            *,
            retry: bool = False,
            metadata: Mapping[str, str] = {},
            timeout: Optional[timedelta] = None,
        ) -> temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse:
            raise self.already_exists_err

    with mock.patch.object(
        client.workflow_service, "start_workflow_execution", start_workflow_execution()
    ):
        with pytest.raises(RPCError) as err:
            await client.start_workflow("fake", id="fake", task_queue="fake")
    assert err.value.status == RPCStatusCode.ALREADY_EXISTS


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
    with pytest.raises(WorkflowQueryFailedError) as err:
        await handle.query("does not exist")


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
    config["default_workflow_query_reject_condition"] = (
        temporalio.common.QueryRejectCondition.NOT_OPEN
    )
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

    async def start_workflow_update(
        self, input: StartWorkflowUpdateInput
    ) -> WorkflowUpdateHandle[Any]:
        self._parent.traces.append(("start_workflow_update", input))
        return await super().start_workflow_update(input)


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

    # Verify listing can limit results
    limited = [
        w async for w in client.list_workflows(f"WorkflowId = '{workflow_id}'", limit=3)
    ]
    assert len(limited) == 3
    # With a weird page size
    limited = [
        w
        async for w in client.list_workflows(
            f"WorkflowId = '{workflow_id}'", page_size=2, limit=3
        )
    ]
    assert len(limited) == 3


@workflow.defn
class CountableWorkflow:
    @workflow.run
    async def run(self, wait_forever: bool) -> None:
        await workflow.wait_condition(lambda: not wait_forever)


async def test_count_workflows(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support newer workflow listing")

    # 3 workflows that complete, 2 that don't
    async with new_worker(client, CountableWorkflow) as worker:
        for _ in range(3):
            await client.execute_workflow(
                CountableWorkflow.run,
                False,
                id=f"id-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        for _ in range(2):
            await client.start_workflow(
                CountableWorkflow.run,
                True,
                id=f"id-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    async def fetch_count() -> WorkflowExecutionCount:
        resp = await client.count_workflows(
            f"TaskQueue = '{worker.task_queue}' GROUP BY ExecutionStatus"
        )
        cast(List[WorkflowExecutionCountAggregationGroup], resp.groups).sort(
            key=lambda g: g.count
        )
        return resp

    await assert_eq_eventually(
        WorkflowExecutionCount(
            count=5,
            groups=[
                WorkflowExecutionCountAggregationGroup(
                    count=2, group_values=["Running"]
                ),
                WorkflowExecutionCountAggregationGroup(
                    count=3, group_values=["Completed"]
                ),
            ],
        ),
        fetch_count,
    )


def test_history_from_json():
    # Take proto, make JSON, convert to dict, alter some enums, confirm that it
    # alters the enums back and matches original history

    # Make history with some enums, one one each event
    history = History()
    history.events.add().request_cancel_external_workflow_execution_failed_event_attributes.cause = CancelExternalWorkflowExecutionFailedCause.CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    history.events.add().workflow_execution_started_event_attributes.initiator = (
        ContinueAsNewInitiator.CONTINUE_AS_NEW_INITIATOR_CRON_SCHEDULE
    )
    history.events.add().event_type = (
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.parent_close_policy = ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    history.events.add().workflow_execution_failed_event_attributes.retry_state = (
        RetryState.RETRY_STATE_CANCEL_REQUESTED
    )
    history.events.add().signal_external_workflow_execution_failed_event_attributes.cause = SignalExternalWorkflowExecutionFailedCause.SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    history.events.add().start_child_workflow_execution_failed_event_attributes.cause = StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND
    history.events.add().workflow_execution_started_event_attributes.task_queue.kind = (
        TaskQueueKind.TASK_QUEUE_KIND_NORMAL
    )
    history.events.add().workflow_task_timed_out_event_attributes.timeout_type = (
        TimeoutType.TIMEOUT_TYPE_HEARTBEAT
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.workflow_id_reuse_policy = WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    history.events.add().workflow_task_failed_event_attributes.cause = (
        WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_BAD_BINARY
    )
    history.events.add().workflow_execution_started_event_attributes.continued_failure.timeout_failure_info.timeout_type = TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
    history.events.add().activity_task_started_event_attributes.last_failure.activity_failure_info.retry_state = RetryState.RETRY_STATE_IN_PROGRESS
    history.events.add().workflow_execution_failed_event_attributes.failure.cause.child_workflow_execution_failure_info.retry_state = RetryState.RETRY_STATE_INTERNAL_SERVER_ERROR

    # Convert to JSON dict and alter enums to pascal versions
    bad_history_dict = json_format.MessageToDict(history)
    e = bad_history_dict["events"]
    e[0]["requestCancelExternalWorkflowExecutionFailedEventAttributes"]["cause"] = (
        "ExternalWorkflowExecutionNotFound"
    )
    e[1]["workflowExecutionStartedEventAttributes"]["initiator"] = "CronSchedule"
    e[2]["eventType"] = "ActivityTaskCancelRequested"
    e[3]["startChildWorkflowExecutionInitiatedEventAttributes"]["parentClosePolicy"] = (
        "Abandon"
    )
    e[4]["workflowExecutionFailedEventAttributes"]["retryState"] = "CancelRequested"
    e[5]["signalExternalWorkflowExecutionFailedEventAttributes"]["cause"] = (
        "ExternalWorkflowExecutionNotFound"
    )
    e[6]["startChildWorkflowExecutionFailedEventAttributes"]["cause"] = (
        "NamespaceNotFound"
    )
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


async def test_schedule_basics(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    elif os.getenv("TEMPORAL_TEST_PROTO3"):
        pytest.skip("Older proto library cannot compare repeated fields")
    await assert_no_schedules(client)

    # Create a schedule with a lot of stuff
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "kitchen_sink",
            KSWorkflowParams(actions=[KSAction(result=KSResultAction("some result"))]),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(hours=1),
            run_timeout=timedelta(hours=2),
            task_timeout=timedelta(hours=3),
            retry_policy=RetryPolicy(maximum_attempts=20),
            memo={"memokey1": "memoval1"},
            static_summary="summary",
            static_details="details",
        ),
        spec=ScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(
                    # Intentionally set step 1 though 0 and 1 are the same to prove
                    # that step comes back as sent not as defaulted (other 0 values
                    # for step don't come back as 1)
                    second=(ScheduleRange(1, step=1),),
                    minute=(ScheduleRange(2, 3),),
                    hour=(ScheduleRange(4, 5, 6),),
                    day_of_month=(ScheduleRange(7),),
                    month=(ScheduleRange(9),),
                    year=(ScheduleRange(2080),),
                    # Intentionally leave day of week absent to check default
                    # day_of_week=[ScheduleRange(1)],
                    comment="spec comment 1",
                )
            ],
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(days=10),
                    offset=timedelta(days=2),
                )
            ],
            cron_expressions=["0 12 * * MON"],
            skip=[ScheduleCalendarSpec(year=(ScheduleRange(2050),))],
            start_at=datetime(2060, 7, 8, 9, 10, 11, tzinfo=timezone.utc),
            jitter=timedelta(seconds=80),
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.BUFFER_ONE,
            catchup_window=timedelta(minutes=5),
            pause_on_failure=True,
        ),
        state=ScheduleState(
            note="sched note 1", paused=True, limited_actions=True, remaining_actions=30
        ),
    )
    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        schedule,
        memo={"memokey2": "memoval2"},
    )

    # Alter the schedule to be the expected from server
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    # Args are encoded
    schedule.action.args = await DataConverter.default.encode(schedule.action.args)
    # Retry policy has maximum interval defaulted
    assert schedule.action.retry_policy
    schedule.action.retry_policy.maximum_interval = timedelta(seconds=100)
    # Memo is encoded
    assert schedule.action.memo
    schedule.action.memo = {
        k: (await DataConverter.default.encode([v]))[0]
        for k, v in schedule.action.memo.items()
    }
    # Cron expression becomes calendar spec
    schedule.spec.cron_expressions = []
    assert isinstance(schedule.spec.calendars, list)
    schedule.spec.calendars.append(
        ScheduleCalendarSpec(
            second=(ScheduleRange(0),),
            minute=(ScheduleRange(0),),
            hour=(ScheduleRange(12),),
            day_of_month=(ScheduleRange(1, 31),),
            month=(ScheduleRange(1, 12),),
            day_of_week=(ScheduleRange(1),),
        )
    )
    # Summary & description are encoded
    assert schedule.action.static_summary
    assert schedule.action.static_details
    schedule.action.static_summary = (
        await DataConverter.default.encode([schedule.action.static_summary])
    )[0]
    schedule.action.static_details = (
        await DataConverter.default.encode([schedule.action.static_details])
    )[0]

    # Describe it and confirm
    desc = await handle.describe()
    assert desc.id == handle.id
    assert desc.schedule == schedule
    assert "memoval2" == await desc.memo_value("memokey2")

    # Update to just change the schedule workflow's task timeout
    def update_schedule_simple(input: ScheduleUpdateInput) -> ScheduleUpdate:
        assert input.description.schedule == schedule
        assert isinstance(
            input.description.schedule.action, ScheduleActionStartWorkflow
        )
        input.description.schedule.action.task_timeout = timedelta(minutes=7)
        return ScheduleUpdate(schedule=input.description.schedule)

    await handle.update(update_schedule_simple)
    desc = await handle.describe()
    assert isinstance(desc.schedule.action, ScheduleActionStartWorkflow)
    assert desc.schedule.action.task_timeout == timedelta(minutes=7)

    # Update but cancel update
    expected_update_time = desc.info.last_updated_at
    await handle.update(lambda input: None)
    assert expected_update_time == (await handle.describe()).info.last_updated_at

    # Update but error
    with pytest.raises(RuntimeError) as err:

        def update_fail(input: ScheduleUpdateInput) -> ScheduleUpdate:
            raise RuntimeError("Oh no")

        await handle.update(update_fail)
    assert str(err.value) == "Oh no"
    assert expected_update_time == (await handle.describe()).info.last_updated_at

    # Update it do only be a schedule with simple defaults using async def
    new_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "kitchen_sink",
            KSWorkflowParams(actions=[KSAction(result=KSResultAction("some result"))]),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        ),
        spec=ScheduleSpec(),
        state=ScheduleState(paused=True),
    )
    assert isinstance(new_schedule.action, ScheduleActionStartWorkflow)

    async def update_schedule_basic(input: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(new_schedule)

    await handle.update(update_schedule_basic)
    desc = await handle.describe()
    new_schedule.action.args = await DataConverter.default.encode(
        new_schedule.action.args
    )
    assert desc.schedule == new_schedule

    # Attempt to create duplicate
    with pytest.raises(ScheduleAlreadyRunningError):
        await client.create_schedule(handle.id, new_schedule)

    # Confirm paused
    assert desc.schedule.state.paused
    # Pause and confirm still paused
    await handle.pause()
    desc = await handle.describe()
    assert desc.schedule.state.paused
    assert desc.schedule.state.note == "Paused via Python SDK"
    # Unpause
    await handle.unpause()
    desc = await handle.describe()
    assert not desc.schedule.state.paused
    assert desc.schedule.state.note == "Unpaused via Python SDK"
    # Pause with custom message
    await handle.pause(note="test1")
    desc = await handle.describe()
    assert desc.schedule.state.paused
    assert desc.schedule.state.note == "test1"
    # Unpause with custom message
    await handle.unpause(note="test2")
    desc = await handle.describe()
    assert not desc.schedule.state.paused
    assert desc.schedule.state.note == "test2"

    # Trigger
    assert desc.info.num_actions == 0
    await handle.trigger()

    async def update_desc_get_action_count() -> int:
        nonlocal desc
        desc = await handle.describe()
        return desc.info.num_actions

    await assert_eq_eventually(1, update_desc_get_action_count)
    # Get workflow run and check its result
    action_exec = desc.info.recent_actions[0].action
    assert isinstance(action_exec, ScheduleActionExecutionStartWorkflow)
    assert (
        "some result"
        == await client.get_workflow_handle(
            action_exec.workflow_id, run_id=action_exec.first_execution_run_id
        ).result()
    )

    # Create 4 more schedules of the same type and confirm they are in list
    # eventually. Two of them we will create with search attributes.
    keyword_attr_key = SearchAttributeKey.for_keyword("python-test-schedule-keyword")
    await ensure_search_attributes_present(client, keyword_attr_key)
    expected_ids = [handle.id]
    for i in range(4):
        new_handle = await client.create_schedule(
            f"{handle.id}-{i + 1}",
            desc.schedule,
            search_attributes=TypedSearchAttributes(
                [SearchAttributePair(keyword_attr_key, "some-schedule-attr")]
            )
            if i >= 2
            else None,
        )
        expected_ids.append(new_handle.id)

    async def list_ids() -> List[str]:
        return sorted(
            [
                list_desc.id
                async for list_desc in await client.list_schedules(page_size=2)
            ]
        )

    await assert_eq_eventually(expected_ids, list_ids)

    # Now do a list w/ query for certain search attributes and confirm
    list_descs = [
        d
        async for d in await client.list_schedules(
            "`python-test-schedule-keyword` = 'some-schedule-attr'"
        )
    ]
    assert len(list_descs) == 2
    assert list_descs[0].id in [f"{handle.id}-3", f"{handle.id}-4"]
    assert list_descs[1].id in [f"{handle.id}-3", f"{handle.id}-4"]

    # Delete all of the schedules
    for id in await list_ids():
        await client.get_schedule_handle(id).delete()
    await assert_no_schedules(client)


async def test_schedule_calendar_spec_defaults(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    await assert_no_schedules(client)

    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(
            action=ScheduleActionStartWorkflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[KSAction(result=KSResultAction("some result"))]
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            ),
            spec=ScheduleSpec(calendars=[ScheduleCalendarSpec()]),
            state=ScheduleState(paused=True),
        ),
    )
    desc = await handle.describe()
    assert desc.schedule.spec.calendars[0] == ScheduleCalendarSpec()
    # Make sure that every next time has all zero time portion and is one day
    # after the previous
    assert len(desc.info.next_action_times) == 10
    for i, time in enumerate(desc.info.next_action_times):
        assert time.second == 0
        assert time.minute == 0
        assert time.hour == 0
        if i > 0:
            assert time == desc.info.next_action_times[i - 1] + timedelta(days=1)

    await handle.delete()
    await assert_no_schedules(client)


async def test_schedule_trigger_immediately(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    await assert_no_schedules(client)

    # Create paused schedule that triggers immediately
    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(
            action=ScheduleActionStartWorkflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[KSAction(result=KSResultAction("some result"))]
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            ),
            spec=ScheduleSpec(),
            state=ScheduleState(paused=True),
        ),
        trigger_immediately=True,
    )

    # Confirm workflow result
    desc = await handle.describe()
    assert desc.info.num_actions == 1
    action_exec = (await handle.describe()).info.recent_actions[0].action
    assert isinstance(action_exec, ScheduleActionExecutionStartWorkflow)
    assert (
        "some result"
        == await client.get_workflow_handle(
            action_exec.workflow_id, run_id=action_exec.first_execution_run_id
        ).result()
    )

    await handle.delete()
    await assert_no_schedules(client)


async def test_schedule_backfill(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    await assert_no_schedules(client)

    begin = datetime(year=2020, month=1, day=20, hour=5)

    # Create paused schedule that runs every minute and has two backfills
    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(
            action=ScheduleActionStartWorkflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[KSAction(result=KSResultAction("some result"))]
                ),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(minutes=1))],
            ),
            state=ScheduleState(paused=True),
        ),
        backfill=[
            # 2 actions on Server >= 1.24, 1 action on Server < 1.24.  Older
            # servers backfill workflows in the interval [start_at, end_at), but
            # newer servers backfill the interval [start_at, end_at].
            ScheduleBackfill(
                start_at=begin - timedelta(minutes=30),
                end_at=begin - timedelta(minutes=29),
                overlap=ScheduleOverlapPolicy.ALLOW_ALL,
            )
        ],
    )
    # We accept both 1.24 and pre-1.24 action counts
    assert (await handle.describe()).info.num_actions in [1, 2]

    # Add two more backfills and and -2m will be deduped
    await handle.backfill(
        # 3 actions on Server >= 1.24, 2 actions on Server < 1.24
        ScheduleBackfill(
            start_at=begin - timedelta(minutes=4),
            end_at=begin - timedelta(minutes=2),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
        # 3 actions on Server >= 1.24, 2 actions on Server < 1.24, except on
        # Server >= 1.24, there is overlap with the prior backfill, so this is
        # only net +2 actions, regardless of Server version.
        ScheduleBackfill(
            start_at=begin - timedelta(minutes=2),
            end_at=begin,
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
    )
    assert (await handle.describe()).info.num_actions in [5, 7]

    await handle.delete()
    await assert_no_schedules(client)


async def test_schedule_create_limited_actions_validation(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    sched = Schedule(
        action=ScheduleActionStartWorkflow(
            "some workflow",
            [],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        ),
        spec=ScheduleSpec(),
    )
    with pytest.raises(ValueError) as err:
        sched.state.limited_actions = True
        await client.create_schedule(f"schedule-{uuid.uuid4()}", sched)
    assert "are no remaining actions set" in str(err.value)
    with pytest.raises(ValueError) as err:
        sched.state.limited_actions = False
        sched.state.remaining_actions = 10
        await client.create_schedule(f"schedule-{uuid.uuid4()}", sched)
    assert "are remaining actions set" in str(err.value)


async def test_schedule_workflow_search_attribute_update(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    await assert_no_schedules(client)

    # Put search attribute on server
    text_attr_key = SearchAttributeKey.for_text("python-test-schedule-text")
    untyped_keyword_key = SearchAttributeKey.for_keyword("python-test-schedule-keyword")
    await ensure_search_attributes_present(client, text_attr_key, untyped_keyword_key)

    # Create a schedule with search attributes on the schedule and on the
    # workflow
    create_action = ScheduleActionStartWorkflow(
        "some workflow",
        [],
        id=f"workflow-{uuid.uuid4()}",
        task_queue=f"tq-{uuid.uuid4()}",
        typed_search_attributes=TypedSearchAttributes(
            [SearchAttributePair(text_attr_key, "some-workflow-attr1")]
        ),
    )
    # To test untyped search attributes, we'll manually put them on the action
    create_action.untyped_search_attributes = {
        untyped_keyword_key.name: ["some-untyped-attr1"]
    }
    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(action=create_action, spec=ScheduleSpec()),
        search_attributes=TypedSearchAttributes(
            [SearchAttributePair(text_attr_key, "some-schedule-attr1")]
        ),
    )

    # Do update of typed attrs
    def update_schedule_typed_attrs(
        input: ScheduleUpdateInput,
    ) -> Optional[ScheduleUpdate]:
        assert isinstance(
            input.description.schedule.action, ScheduleActionStartWorkflow
        )

        # Make sure the search attributes are present in all forms
        assert input.description.search_attributes[text_attr_key.name] == [
            "some-schedule-attr1"
        ]
        assert (
            input.description.typed_search_attributes[text_attr_key]
            == "some-schedule-attr1"
        )
        # This assertion has changed since server 1.24. Now, even untyped search
        # attributes are given a type server side
        assert (
            input.description.schedule.action.typed_search_attributes
            and len(input.description.schedule.action.typed_search_attributes) == 2
            and input.description.schedule.action.typed_search_attributes[text_attr_key]
            == "some-workflow-attr1"
            and input.description.schedule.action.typed_search_attributes[
                untyped_keyword_key
            ]
            == "some-untyped-attr1"
        )

        # Update the workflow search attribute with a new typed value but does
        # not change the untyped value
        return ScheduleUpdate(
            dataclasses.replace(
                input.description.schedule,
                action=dataclasses.replace(
                    input.description.schedule.action,
                    typed_search_attributes=input.description.schedule.action.typed_search_attributes.updated(
                        SearchAttributePair(text_attr_key, "some-workflow-attr2")
                    ),
                ),
            )
        )

    await handle.update(update_schedule_typed_attrs)

    # Check that it changed
    desc = await handle.describe()
    assert isinstance(desc.schedule.action, ScheduleActionStartWorkflow)
    # Check that the workflow search attributes were changed
    # This assertion has changed since server 1.24. Now, even untyped search
    # attributes are given a type server side
    assert (
        desc.schedule.action.typed_search_attributes
        and len(desc.schedule.action.typed_search_attributes) == 2
        and desc.schedule.action.typed_search_attributes[text_attr_key]
        == "some-workflow-attr2"
        and desc.schedule.action.typed_search_attributes[untyped_keyword_key]
        == "some-untyped-attr1"
    )
    # Check that the schedule search attributes were not changed
    assert desc.search_attributes[text_attr_key.name] == ["some-schedule-attr1"]
    assert desc.typed_search_attributes[text_attr_key] == "some-schedule-attr1"

    await handle.delete()
    await assert_no_schedules(client)


@pytest.mark.parametrize(
    "test_case",
    [
        "none-is-noop",
        "empty-but-non-none-clears",
        "all-new-values-overwrites",
        "partial-new-values-overwrites-and-drops",
    ],
)
async def test_schedule_search_attribute_update(
    client: Client, env: WorkflowEnvironment, test_case: str
):
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support schedules")
    await assert_no_schedules(client)

    # Put search attributes on server
    key_1 = SearchAttributeKey.for_text("python-test-schedule-sa-update-key-1")
    key_2 = SearchAttributeKey.for_keyword("python-test-schedule-sa-update-key-2")
    await ensure_search_attributes_present(client, key_1, key_2)
    val_1 = "val-1"
    val_2 = "val-2"

    # Create a schedule with search attributes
    create_action = ScheduleActionStartWorkflow(
        "some workflow",
        [],
        id=f"workflow-{uuid.uuid4()}",
        task_queue=f"tq-{uuid.uuid4()}",
    )
    handle = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(action=create_action, spec=ScheduleSpec()),
        search_attributes=TypedSearchAttributes(
            [
                SearchAttributePair(key_1, val_1),
                SearchAttributePair(key_2, val_2),
            ]
        ),
    )

    def update_search_attributes(
        input: ScheduleUpdateInput,
    ) -> Optional[ScheduleUpdate]:
        # Make sure the initial search attributes are present
        assert input.description.search_attributes[key_1.name] == [val_1]
        assert input.description.search_attributes[key_2.name] == [val_2]
        assert input.description.typed_search_attributes[key_1] == val_1
        assert input.description.typed_search_attributes[key_2] == val_2

        if test_case == "none-is-noop":
            # Passing None makes no changes
            return ScheduleUpdate(input.description.schedule, search_attributes=None)
        elif test_case == "empty-but-non-none-clears":
            # Pass empty but non-None to clear all attributes
            return ScheduleUpdate(
                input.description.schedule,
                search_attributes=TypedSearchAttributes.empty,
            )
        elif test_case == "all-new-values-overwrites":
            # Pass all new values to overwrite existing
            return ScheduleUpdate(
                input.description.schedule,
                search_attributes=input.description.typed_search_attributes.updated(
                    SearchAttributePair(key_1, val_1 + "-new"),
                    SearchAttributePair(key_2, val_2 + "-new"),
                ),
            )
        elif test_case == "partial-new-values-overwrites-and-drops":
            # Only update key_1, which should drop key_2
            return ScheduleUpdate(
                input.description.schedule,
                search_attributes=TypedSearchAttributes(
                    [
                        SearchAttributePair(key_1, val_1 + "-new"),
                    ]
                ),
            )
        else:
            raise ValueError(f"Invalid test case: {test_case}")

    await handle.update(update_search_attributes)

    if test_case == "none-is-noop":

        async def expectation() -> bool:
            desc = await handle.describe()
            return (
                desc.search_attributes[key_1.name] == [val_1]
                and desc.search_attributes[key_2.name] == [val_2]
                and desc.typed_search_attributes[key_1] == val_1
                and desc.typed_search_attributes[key_2] == val_2
            )

        await assert_eq_eventually(True, expectation)
    elif test_case == "empty-but-non-none-clears":

        async def expectation() -> bool:
            desc = await handle.describe()
            return (
                len(desc.typed_search_attributes) == 0
                and len(desc.search_attributes) == 0
            )

        await assert_eq_eventually(True, expectation)
    elif test_case == "all-new-values-overwrites":

        async def expectation() -> bool:
            desc = await handle.describe()
            return (
                desc.search_attributes[key_1.name] == [val_1 + "-new"]
                and desc.search_attributes[key_2.name] == [val_2 + "-new"]
                and desc.typed_search_attributes[key_1] == val_1 + "-new"
                and desc.typed_search_attributes[key_2] == val_2 + "-new"
            )

        await assert_eq_eventually(True, expectation)
    elif test_case == "partial-new-values-overwrites-and-drops":

        async def expectation() -> bool:
            desc = await handle.describe()
            return (
                desc.search_attributes[key_1.name] == [val_1 + "-new"]
                and desc.typed_search_attributes[key_1] == val_1 + "-new"
                and key_2.name not in desc.search_attributes
                and key_2 not in desc.typed_search_attributes
            )

        await assert_eq_eventually(True, expectation)
    else:
        raise ValueError(f"Invalid test case: {test_case}")

    await handle.delete()
    await assert_no_schedules(client)


async def assert_no_schedules(client: Client) -> None:
    # Listing appears eventually consistent
    async def schedule_count() -> int:
        return len([d async for d in await client.list_schedules()])

    await assert_eq_eventually(0, schedule_count)


async def test_build_id_interactions(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support worker versioning")
    if not await worker_versioning_enabled(client):
        pytest.skip("This server does not have worker versioning enabled")

    tq = "test-build-id-interactions_" + str(uuid.uuid4())

    await client.update_worker_build_id_compatibility(tq, BuildIdOpAddNewDefault("1.0"))
    await client.update_worker_build_id_compatibility(
        tq, BuildIdOpAddNewCompatible("1.1", "1.0")
    )
    sets = await client.get_worker_build_id_compatibility(tq)
    assert sets.default_build_id() == "1.1"
    assert sets.default_set().build_ids[0] == "1.0"

    await client.update_worker_build_id_compatibility(
        tq, BuildIdOpPromoteBuildIdWithinSet("1.0")
    )
    sets = await client.get_worker_build_id_compatibility(tq)
    assert sets.default_build_id() == "1.0"

    await client.update_worker_build_id_compatibility(tq, BuildIdOpAddNewDefault("2.0"))
    sets = await client.get_worker_build_id_compatibility(tq)
    assert sets.default_build_id() == "2.0"

    await client.update_worker_build_id_compatibility(
        tq, BuildIdOpPromoteSetByBuildId("1.0")
    )
    sets = await client.get_worker_build_id_compatibility(tq)
    assert sets.default_build_id() == "1.0"

    await client.update_worker_build_id_compatibility(
        tq, BuildIdOpMergeSets(primary_build_id="2.0", secondary_build_id="1.0")
    )
    sets = await client.get_worker_build_id_compatibility(tq)
    assert sets.default_build_id() == "2.0"

    reachability = await client.get_worker_task_reachability(
        build_ids=["2.0", "1.0", "1.1"]
    )
    assert reachability.build_id_reachability["2.0"].task_queue_reachability[tq] == [
        TaskReachabilityType.NEW_WORKFLOWS
    ]
    assert reachability.build_id_reachability["1.0"].task_queue_reachability[tq] == []
    assert reachability.build_id_reachability["1.1"].task_queue_reachability[tq] == []


async def test_cloud_client_simple():
    if "TEMPORAL_CLIENT_CLOUD_API_KEY" not in os.environ:
        pytest.skip("No cloud API key")
    client = await CloudOperationsClient.connect(
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
        version=os.environ["TEMPORAL_CLIENT_CLOUD_API_VERSION"],
    )
    result = await client.cloud_service.get_namespace(
        GetNamespaceRequest(namespace=os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"])
    )
    assert os.environ["TEMPORAL_CLIENT_CLOUD_NAMESPACE"] == result.namespace.namespace
