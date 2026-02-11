import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import (
    ActivityExecutionCount,
    ActivityExecutionCountAggregationGroup,
    ActivityExecutionDescription,
    ActivityExecutionStatus,
    ActivityFailureError,
    ActivityHandle,
    CancelActivityInput,
    Client,
    CountActivitiesInput,
    DescribeActivityInput,
    Interceptor,
    ListActivitiesInput,
    OutboundInterceptor,
    PendingActivityState,
    StartActivityInput,
    TerminateActivityInput,
)
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually


@activity.defn
async def increment(input: int) -> int:
    return input + 1


# Activity classes for testing start_activity_class / execute_activity_class
@activity.defn
class IncrementClass:
    """Async callable class activity with a parameter."""

    async def __call__(self, x: int) -> int:
        return x + 1


@activity.defn
class NoParamClass:
    """Async callable class activity with no parameters."""

    async def __call__(self) -> str:
        return "no-param-result"


@activity.defn
class SyncIncrementClass:
    """Sync callable class activity with a parameter."""

    def __call__(self, x: int) -> int:
        return x + 1


# Activity holder for testing start_activity_method / execute_activity_method
class ActivityHolder:
    """Class holding activity methods."""

    @activity.defn
    async def async_increment(self, x: int) -> int:
        return x + 1

    @activity.defn
    async def async_no_param(self) -> str:
        return "async-method-result"

    @activity.defn
    def sync_increment(self, x: int) -> int:
        return x + 1


class TestDescribe:
    @pytest.fixture
    async def activity_handle(self, client: Client, env: WorkflowEnvironment):
        if env.supports_time_skipping:
            pytest.skip(
                "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
            )

        id = str(uuid.uuid4())
        task_queue = str(uuid.uuid4())
        yield await client.start_activity(
            increment,
            args=(42,),
            id=id,
            task_queue=task_queue,
            schedule_to_close_timeout=timedelta(hours=1),
        )

    async def test_describe(self, client: Client, activity_handle: ActivityHandle):
        desc = await activity_handle.describe()
        # From ActivityExecution (base class)
        assert desc.activity_id == activity_handle.id
        assert desc.activity_run_id == activity_handle.run_id
        assert desc.activity_type == "increment"
        assert desc.close_time is None  # not closed yet
        assert desc.execution_duration is None  # not closed yet
        assert desc.namespace == client.namespace
        assert desc.raw_info is not None
        assert desc.scheduled_time is not None
        assert len(desc.typed_search_attributes) == 0
        assert desc.state_transition_count is not None
        assert desc.status == ActivityExecutionStatus.RUNNING
        assert desc.task_queue
        # From ActivityExecutionDescription
        assert desc.attempt == 1
        assert desc.canceled_reason is None
        assert desc.current_retry_interval is None
        assert desc.eager_execution_requested is False
        assert desc.expiration_time is not None
        assert len(desc.raw_heartbeat_details) == 0
        assert desc.run_state == PendingActivityState.SCHEDULED
        assert desc.last_attempt_complete_time is None
        assert desc.last_failure is None
        assert desc.last_heartbeat_time is None
        assert desc.last_started_time is None
        assert desc.last_worker_identity == ""
        assert desc.long_poll_token is not None
        assert desc.next_attempt_schedule_time is None
        assert desc.paused is False
        assert desc.retry_policy is not None

    async def test_describe_long_poll(self, activity_handle: ActivityHandle):
        desc1 = await activity_handle.describe()
        assert desc1.long_poll_token
        desc2_task = asyncio.create_task(
            activity_handle.describe(long_poll_token=desc1.long_poll_token)
        )
        # Worker poll causes a transition to Started which notifies the waiting long-poll.
        async with Worker(
            activity_handle._client,
            task_queue=desc1.task_queue,
            activities=[increment],
        ):
            desc2 = await desc2_task
            assert desc2.state_transition_count and desc1.state_transition_count
            assert desc2.state_transition_count > desc1.state_transition_count


class ActivityTracingInterceptor(Interceptor):
    """Test interceptor that tracks all activity interceptor calls."""

    def __init__(self) -> None:
        super().__init__()
        self.start_activity_calls: list[StartActivityInput] = []
        self.describe_activity_calls: list[DescribeActivityInput] = []
        self.cancel_activity_calls: list[CancelActivityInput] = []
        self.terminate_activity_calls: list[TerminateActivityInput] = []
        self.list_activities_calls: list[ListActivitiesInput] = []
        self.count_activities_calls: list[CountActivitiesInput] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return ActivityTracingOutboundInterceptor(self, next)


class ActivityTracingOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self,
        parent: ActivityTracingInterceptor,
        next: OutboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def start_activity(self, input: StartActivityInput):
        assert isinstance(input, StartActivityInput)
        self._parent.start_activity_calls.append(input)
        return await super().start_activity(input)

    async def describe_activity(self, input: DescribeActivityInput):
        assert isinstance(input, DescribeActivityInput)
        self._parent.describe_activity_calls.append(input)
        return await super().describe_activity(input)

    async def cancel_activity(self, input: CancelActivityInput):
        assert isinstance(input, CancelActivityInput)
        self._parent.cancel_activity_calls.append(input)
        return await super().cancel_activity(input)

    async def terminate_activity(self, input: TerminateActivityInput):
        assert isinstance(input, TerminateActivityInput)
        self._parent.terminate_activity_calls.append(input)
        return await super().terminate_activity(input)

    def list_activities(self, input: ListActivitiesInput):
        assert isinstance(input, ListActivitiesInput)
        self._parent.list_activities_calls.append(input)
        return super().list_activities(input)

    async def count_activities(self, input: CountActivitiesInput):
        assert isinstance(input, CountActivitiesInput)
        self._parent.count_activities_calls.append(input)
        return await super().count_activities(input)


async def test_start_activity_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """Client.start_activity() should call the start_activity interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    assert len(interceptor.start_activity_calls) == 1
    call = interceptor.start_activity_calls[0]
    assert call.id == activity_id
    assert call.task_queue == task_queue
    assert call.activity_type == "increment"


async def test_describe_activity_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """ActivityHandle.describe() should call the describe_activity interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    desc = await activity_handle.describe()
    assert isinstance(desc, ActivityExecutionDescription)

    assert len(interceptor.describe_activity_calls) == 1
    call = interceptor.describe_activity_calls[0]
    assert call.activity_id == activity_id


async def test_cancel_activity_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """ActivityHandle.cancel() should call the cancel_activity interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    await activity_handle.cancel(reason="test cancellation")

    assert len(interceptor.cancel_activity_calls) == 1
    call = interceptor.cancel_activity_calls[0]
    assert call.activity_id == activity_id
    assert call.reason == "test cancellation"


async def test_terminate_activity_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """ActivityHandle.terminate() should call the terminate_activity interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    await activity_handle.terminate(reason="test termination")

    assert len(interceptor.terminate_activity_calls) == 1
    call = interceptor.terminate_activity_calls[0]
    assert call.activity_id == activity_id
    assert call.reason == "test termination"


async def test_list_activities_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """Client.list_activities() should call the list_activities interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    query = f'ActivityId = "{activity_id}"'
    async for _ in intercepted_client.list_activities(query):
        pass

    assert len(interceptor.list_activities_calls) >= 1
    call = interceptor.list_activities_calls[0]
    assert call.query == query


async def test_count_activities_calls_interceptor(
    client: Client, env: WorkflowEnvironment
):
    """Client.count_activities() should call the count_activities interceptor."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    interceptor = ActivityTracingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    await intercepted_client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    query = f'ActivityId = "{activity_id}"'
    count = await intercepted_client.count_activities(query)
    assert isinstance(count, ActivityExecutionCount)

    assert len(interceptor.count_activities_calls) == 1
    call = interceptor.count_activities_calls[0]
    assert call.query == query


async def test_get_result(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    result_via_execute_activity = client.execute_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[increment],
    ):
        assert await activity_handle.result() == 2
        assert await result_via_execute_activity == 2


async def test_get_activity_handle(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    handle_by_id = client.get_activity_handle(activity_id)
    assert handle_by_id.id == activity_id
    assert handle_by_id.run_id is None

    handle_by_id_and_run_id = client.get_activity_handle(
        activity_id,
        run_id=activity_handle.run_id,
    )
    assert handle_by_id_and_run_id.id == activity_id
    assert handle_by_id_and_run_id.run_id == activity_handle.run_id

    handle_with_result_type = client.get_activity_handle(
        activity_id,
        run_id=activity_handle.run_id,
        result_type=int,
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[increment],
    ):
        assert await handle_by_id.result() == 2
        assert await handle_by_id_and_run_id.result() == 2
        assert await handle_with_result_type.result() == 2


async def test_list_activities(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    executions = [
        e async for e in client.list_activities(f'ActivityId = "{activity_id}"')
    ]
    assert len(executions) == 1
    execution = executions[0]
    assert execution.activity_id == activity_id
    assert execution.activity_type == "increment"
    assert execution.task_queue == task_queue
    assert execution.status == ActivityExecutionStatus.RUNNING
    assert execution.state_transition_count is None  # Not set until activity completes


async def test_count_activities(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async def fetch_count():
        return await client.count_activities(f'ActivityId = "{activity_id}"')

    await assert_eq_eventually(
        ActivityExecutionCount(count=1, groups=[]),
        fetch_count,
    )


async def test_count_activities_group_by(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    from temporalio.client import ActivityExecutionCount

    task_queue = str(uuid.uuid4())
    activity_ids = []

    for _ in range(3):
        activity_id = str(uuid.uuid4())
        activity_ids.append(activity_id)
        await client.start_activity(
            increment,
            1,
            id=activity_id,
            task_queue=task_queue,
            schedule_to_close_timeout=timedelta(seconds=60),
        )

    ids_filter = " OR ".join([f'ActivityId = "{aid}"' for aid in activity_ids])

    async def fetch_count() -> ActivityExecutionCount:
        return await client.count_activities(f"({ids_filter}) GROUP BY ExecutionStatus")

    await assert_eq_eventually(
        ActivityExecutionCount(
            count=3,
            groups=[
                ActivityExecutionCountAggregationGroup(
                    count=3, group_values=["Running"]
                ),
            ],
        ),
        fetch_count,
    )


@dataclass
class ActivityInput:
    event_workflow_id: str
    wait_for_activity_start_workflow_id: str | None = None


@activity.defn
async def async_activity(input: ActivityInput) -> int:
    # Notify test that the activity has started and is ready to be completed manually
    await (
        activity.client()
        .get_workflow_handle(input.event_workflow_id)
        .signal(EventWorkflow.set)
    )
    activity.raise_complete_async()


async def test_manual_completion(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    event_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        ActivityInput(event_workflow_id=event_workflow_id),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[EventWorkflow],
    ):
        # Wait for activity to start
        await client.execute_workflow(
            EventWorkflow.wait,
            id=event_workflow_id,
            task_queue=task_queue,
        )
        # Complete activity manually
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await async_activity_handle.complete(7)
        assert await activity_handle.result() == 7

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.COMPLETED


async def test_manual_cancellation(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    event_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        ActivityInput(event_workflow_id=event_workflow_id),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[EventWorkflow],
    ):
        # Wait for activity to start
        await client.execute_workflow(
            EventWorkflow.wait,
            id=event_workflow_id,
            task_queue=task_queue,
        )
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )

        # report_cancellation fails if activity is not in CANCELLATION_REQUESTED state
        with pytest.raises(RPCError) as err:
            await async_activity_handle.report_cancellation("Test cancellation")
        assert err.value.status == RPCStatusCode.FAILED_PRECONDITION
        assert "invalid transition from Started" in str(err.value)

        # Request cancellation to transition activity to CANCELLATION_REQUESTED state
        await activity_handle.cancel()

        # Now report_cancellation succeeds
        await async_activity_handle.report_cancellation("Test cancellation")

        with pytest.raises(ActivityFailureError) as exc_info:
            await activity_handle.result()
        assert isinstance(exc_info.value.cause, CancelledError)
        assert list(exc_info.value.cause.details) == ["Test cancellation"]

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.CANCELED


async def test_manual_failure(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    event_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        ActivityInput(event_workflow_id=event_workflow_id),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[EventWorkflow],
    ):
        await client.execute_workflow(
            EventWorkflow.wait,
            id=event_workflow_id,
            task_queue=task_queue,
        )
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await async_activity_handle.fail(
            ApplicationError("Test failure", non_retryable=True)
        )
        with pytest.raises(ActivityFailureError) as err:
            await activity_handle.result()
        assert isinstance(err.value.cause, ApplicationError)
        assert str(err.value.cause) == "Test failure"

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.FAILED


@activity.defn
async def activity_for_testing_heartbeat(input: ActivityInput) -> str:
    info = activity.info()
    if info.attempt == 1:
        # Signal that activity has started (only on first attempt)
        if input.wait_for_activity_start_workflow_id:
            await (
                activity.client()
                .get_workflow_handle(
                    workflow_id=input.wait_for_activity_start_workflow_id,
                )
                .signal(EventWorkflow.set)
            )
        wait_for_heartbeat_wf_handle = await activity.client().start_workflow(
            EventWorkflow.wait,
            id=input.event_workflow_id,
            task_queue=activity.info().task_queue,
        )
        # Wait for test to notify that it has sent heartbeat
        await wait_for_heartbeat_wf_handle.result()
        raise Exception("Intentional error to force retry")
    elif info.attempt == 2:
        [heartbeat_data] = info.heartbeat_details
        assert isinstance(heartbeat_data, str)
        return heartbeat_data
    else:
        raise AssertionError(f"Unexpected attempt number: {info.attempt}")


async def test_manual_heartbeat(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    event_workflow_id = str(uuid.uuid4())
    wait_for_activity_start_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        activity_for_testing_heartbeat,
        ActivityInput(
            event_workflow_id=event_workflow_id,
            wait_for_activity_start_workflow_id=wait_for_activity_start_workflow_id,
        ),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    wait_for_activity_start_wf_handle = await client.start_workflow(
        EventWorkflow.wait,
        id=wait_for_activity_start_workflow_id,
        task_queue=task_queue,
    )
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[activity_for_testing_heartbeat],
        workflows=[EventWorkflow],
    ):
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await wait_for_activity_start_wf_handle.result()
        await async_activity_handle.heartbeat("Test heartbeat details")
        await client.get_workflow_handle(
            workflow_id=event_workflow_id,
        ).signal(EventWorkflow.set)
        assert await activity_handle.result() == "Test heartbeat details"


async def test_id_conflict_policy_fail(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    from temporalio.common import ActivityIDConflictPolicy
    from temporalio.exceptions import ActivityAlreadyStartedError

    await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=timedelta(seconds=60),
        id_conflict_policy=ActivityIDConflictPolicy.FAIL,
    )

    with pytest.raises(ActivityAlreadyStartedError) as err:
        await client.start_activity(
            increment,
            1,
            id=activity_id,
            task_queue=task_queue,
            schedule_to_close_timeout=timedelta(seconds=60),
            id_conflict_policy=ActivityIDConflictPolicy.FAIL,
        )
    assert err.value.activity_id == activity_id


async def test_id_conflict_policy_use_existing(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    from temporalio.common import ActivityIDConflictPolicy

    handle1 = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=timedelta(seconds=60),
        id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
    )

    handle2 = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=timedelta(seconds=60),
        id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
    )

    assert handle1.id == handle2.id
    assert handle1.run_id == handle2.run_id


async def test_id_reuse_policy_reject_duplicate(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    from temporalio.common import ActivityIDReusePolicy
    from temporalio.exceptions import ActivityAlreadyStartedError

    handle = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
        id_reuse_policy=ActivityIDReusePolicy.REJECT_DUPLICATE,
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[increment],
    ):
        await handle.result()

    with pytest.raises(ActivityAlreadyStartedError) as err:
        await client.start_activity(
            increment,
            1,
            id=activity_id,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=5),
            id_reuse_policy=ActivityIDReusePolicy.REJECT_DUPLICATE,
        )
    assert err.value.activity_id == activity_id


async def test_id_reuse_policy_allow_duplicate(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    from temporalio.common import ActivityIDReusePolicy

    handle1 = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
        id_reuse_policy=ActivityIDReusePolicy.ALLOW_DUPLICATE,
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[increment],
    ):
        await handle1.result()

    handle2 = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
        id_reuse_policy=ActivityIDReusePolicy.ALLOW_DUPLICATE,
    )

    assert handle1.id == handle2.id
    assert handle1.run_id != handle2.run_id


async def test_search_attributes(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    from temporalio.common import (
        SearchAttributeKey,
        SearchAttributePair,
        TypedSearchAttributes,
    )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    temporal_change_version_key = SearchAttributeKey.for_keyword_list(
        "TemporalChangeVersion"
    )

    handle = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        schedule_to_close_timeout=timedelta(seconds=60),
        search_attributes=TypedSearchAttributes(
            [SearchAttributePair(temporal_change_version_key, ["test-1", "test-2"])]
        ),
    )

    desc = await handle.describe()
    assert desc.typed_search_attributes[temporal_change_version_key] == [
        "test-1",
        "test-2",
    ]


async def test_retry_policy(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    from temporalio.common import RetryPolicy

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    handle = await client.start_activity(
        increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=10),
            backoff_coefficient=2.0,
            maximum_attempts=3,
        ),
    )

    desc = await handle.describe()
    assert desc.retry_policy is not None
    assert desc.retry_policy.initial_interval == timedelta(seconds=1)
    assert desc.retry_policy.maximum_interval == timedelta(seconds=10)
    assert desc.retry_policy.backoff_coefficient == 2.0
    assert desc.retry_policy.maximum_attempts == 3


async def test_terminate(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    event_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        args=(ActivityInput(event_workflow_id=event_workflow_id),),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[EventWorkflow],
    ):
        await client.execute_workflow(
            EventWorkflow.wait,
            id=event_workflow_id,
            task_queue=task_queue,
        )

        await activity_handle.terminate(reason="Test termination")

        with pytest.raises(ActivityFailureError):
            await activity_handle.result()

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.TERMINATED


# Tests for start_activity_class / execute_activity_class


async def test_start_activity_class_async(client: Client, env: WorkflowEnvironment):
    """Test start_activity_class with an async callable class."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    handle = await client.start_activity_class(
        IncrementClass,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[IncrementClass()],
    ):
        result = await handle.result()
        assert result == 2


async def test_execute_activity_class_async(client: Client, env: WorkflowEnvironment):
    """Test execute_activity_class with an async callable class."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[IncrementClass()],
    ):
        result = await client.execute_activity_class(
            IncrementClass,
            1,
            id=activity_id,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=5),
        )
        assert result == 2


async def test_start_activity_class_no_param(client: Client, env: WorkflowEnvironment):
    """Test start_activity_class with a no-param callable class."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    handle = await client.start_activity_class(
        NoParamClass,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[NoParamClass()],
    ):
        result = await handle.result()
        assert result == "no-param-result"


async def test_start_activity_class_sync(client: Client, env: WorkflowEnvironment):
    """Test start_activity_class with a sync callable class."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    import concurrent.futures

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    handle = await client.start_activity_class(
        SyncIncrementClass,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            client,
            task_queue=task_queue,
            activities=[SyncIncrementClass()],
            activity_executor=executor,
        ):
            result = await handle.result()
            assert result == 2


# Tests for start_activity_method / execute_activity_method


async def test_start_activity_method_async(client: Client, env: WorkflowEnvironment):
    """Test start_activity_method with an async method."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    holder = ActivityHolder()
    handle = await client.start_activity_method(
        ActivityHolder.async_increment,
        1,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[holder.async_increment],
    ):
        result = await handle.result()
        assert result == 2


async def test_execute_activity_method_async(client: Client, env: WorkflowEnvironment):
    """Test execute_activity_method with an async method."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    holder = ActivityHolder()
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[holder.async_increment],
    ):
        result = await client.execute_activity_method(
            ActivityHolder.async_increment,
            1,
            id=activity_id,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=5),
        )
        assert result == 2


async def test_start_activity_method_no_param(client: Client, env: WorkflowEnvironment):
    """Test start_activity_method with a no-param method."""
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2741"
        )

    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    holder = ActivityHolder()
    handle = await client.start_activity_method(
        ActivityHolder.async_no_param,
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[holder.async_no_param],
    ):
        result = await handle.result()
        assert result == "async-method-result"


# Utilities


@workflow.defn
class EventWorkflow:
    """
    A workflow version of asyncio.Event()
    """

    def __init__(self) -> None:
        self.signal_received = asyncio.Event()

    @workflow.run
    async def wait(self) -> None:
        await self.signal_received.wait()

    @workflow.signal
    def set(self) -> None:
        self.signal_received.set()
