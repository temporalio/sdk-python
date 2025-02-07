import asyncio
import platform
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, List, Optional, Union

import pytest

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    RPCError,
    StartWorkflowInput,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import (
    RetryPolicy,
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    TimeoutError,
    TimeoutType,
)
from temporalio.testing import WorkflowEnvironment
from tests.helpers import new_worker


@workflow.defn
class ReallySlowWorkflow:
    @workflow.run
    async def run(self) -> str:
        await asyncio.sleep(100000)
        return "all done"

    @workflow.query
    def current_time(self) -> float:
        return workflow.now().timestamp()

    @workflow.signal
    async def some_signal(self) -> None:
        pass


def skip_if_not_x86() -> None:
    if platform.machine() not in ("i386", "AMD64", "x86_64"):
        pytest.skip("Time skipping server does not run outside x86")


async def test_workflow_env_time_skipping_basic():
    skip_if_not_x86()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with new_worker(env.client, ReallySlowWorkflow) as worker:
            # Check that time is around now
            assert_timestamp_from_now(await env.get_current_time(), 0)
            # Run workflow
            assert "all done" == await env.client.execute_workflow(
                ReallySlowWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            # Check that the time is around 100000 seconds after now
            assert_timestamp_from_now(await env.get_current_time(), 100000)


async def test_workflow_env_time_skipping_manual():
    skip_if_not_x86()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with new_worker(env.client, ReallySlowWorkflow) as worker:
            # Start workflow
            handle = await env.client.start_workflow(
                ReallySlowWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            async def workflow_current_time() -> float:
                # We send signal first since query timestamp is based on last
                # non-query-only workflow task
                await handle.signal(ReallySlowWorkflow.some_signal)
                return await handle.query(ReallySlowWorkflow.current_time)

            # Confirm query will say we're near current time
            assert_timestamp_from_now(await workflow_current_time(), 0)

            # Sleep and confirm query will say we're near that time
            await env.sleep(1000)
            assert_timestamp_from_now(await workflow_current_time(), 1000)


class Activities:
    def __init__(self, env: WorkflowEnvironment) -> None:
        self.env = env

    @activity.defn
    async def simulate_heartbeat_timeout(self) -> str:
        # Sleep for twice as long as heartbeat timeout
        heartbeat_timeout = activity.info().heartbeat_timeout
        assert heartbeat_timeout
        await self.env.sleep(heartbeat_timeout.total_seconds() * 2)
        return "all done"


@workflow.defn
class ActivityWaitWorkflow:
    @workflow.run
    async def run(self) -> str:
        # Start activity with 20 second heartbeat timeout
        return await workflow.execute_activity_method(
            Activities.simulate_heartbeat_timeout,
            schedule_to_close_timeout=timedelta(seconds=1000),
            heartbeat_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def test_workflow_env_time_skipping_heartbeat_timeout():
    skip_if_not_x86()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with new_worker(
            env.client,
            ActivityWaitWorkflow,
            activities=[Activities(env).simulate_heartbeat_timeout],
        ) as worker:
            with pytest.raises(WorkflowFailureError) as err:
                await env.client.execute_workflow(
                    ActivityWaitWorkflow.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            # Check the causes until heartbeat timeout
            assert isinstance(err.value.cause, ActivityError)
            assert isinstance(err.value.cause.cause, TimeoutError)
            assert err.value.cause.cause.type == TimeoutType.HEARTBEAT


@workflow.defn
class ShortSleepWorkflow:
    @workflow.run
    async def run(self) -> str:
        await asyncio.sleep(3)
        return "all done"


async def test_workflow_env_time_skipping_disabled():
    skip_if_not_x86()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with new_worker(env.client, ShortSleepWorkflow) as worker:
            # Confirm when executing normally it does not sleep for a full 3s
            start = monotonic()
            assert "all done" == await env.client.execute_workflow(
                ShortSleepWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert monotonic() - start < 2.5

            # Confirm when skipping is disabled, it does sleep for a full 3s
            with env.auto_time_skipping_disabled():
                start = monotonic()
                assert "all done" == await env.client.execute_workflow(
                    ShortSleepWorkflow.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                assert monotonic() - start > 2.5


@workflow.defn
class AssertFailWorkflow:
    @workflow.run
    async def run(self, only_signal: bool) -> None:
        if only_signal:
            # Wait forever
            await asyncio.Future()
        else:
            assert "foo" == "bar"

    @workflow.signal
    def some_signal(self) -> None:
        assert "foo" == "bar"


class SimpleClientInterceptor(Interceptor):
    def __init__(self) -> None:
        self.events: List[str] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return SimpleClientOutboundInterceptor(self, super().intercept_client(next))


class SimpleClientOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self, root: SimpleClientInterceptor, next: OutboundInterceptor
    ) -> None:
        super().__init__(next)
        self.root = root

    async def start_workflow(
        self, input: StartWorkflowInput
    ) -> WorkflowHandle[Any, Any]:
        self.root.events.append(f"start: {input.workflow}")
        return await super().start_workflow(input)


async def test_workflow_env_assert(client: Client):
    # Set the interceptor on the client. This used to fail for being
    # accidentally overridden.
    client_config = client.config()
    interceptor = SimpleClientInterceptor()
    client_config["interceptors"] = [interceptor]
    client = Client(**client_config)

    def assert_proper_error(err: Optional[BaseException]) -> None:
        assert isinstance(err, ApplicationError)
        # In unsandboxed workflows, this message has extra diff info appended
        # due to pytest's custom loader that does special assert tricks. But in
        # sandboxed workflows, this just has the first line.
        assert err.message.startswith("assert 'foo' == 'bar'")

    async with WorkflowEnvironment.from_client(client) as env:
        async with new_worker(env.client, AssertFailWorkflow) as worker:
            # Check assertion failure inside of run
            with pytest.raises(WorkflowFailureError) as err:
                await env.client.execute_workflow(
                    AssertFailWorkflow.run,
                    False,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            assert_proper_error(err.value.cause)
            assert interceptor.events

            # Start a new one and check signal
            handle = await env.client.start_workflow(
                AssertFailWorkflow.run,
                True,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle.signal(AssertFailWorkflow.some_signal)
            with pytest.raises(WorkflowFailureError) as err:
                await handle.result()
            assert_proper_error(err.value.cause)


async def test_search_attributes_on_dev_server(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Only testing for local dev server")

    # Search attributes
    sa_prefix = f"{uuid.uuid4()}_"
    text_attr = SearchAttributeKey.for_text(f"{sa_prefix}text")
    keyword_attr = SearchAttributeKey.for_keyword(f"{sa_prefix}keyword")
    keyword_list_attr = SearchAttributeKey.for_keyword_list(f"{sa_prefix}keyword_list")
    int_attr = SearchAttributeKey.for_int(f"{sa_prefix}int")
    float_attr = SearchAttributeKey.for_float(f"{sa_prefix}double")
    bool_attr = SearchAttributeKey.for_bool(f"{sa_prefix}bool")
    datetime_attr = SearchAttributeKey.for_datetime(f"{sa_prefix}datetime")
    attrs = TypedSearchAttributes(
        [
            SearchAttributePair(text_attr, "text1"),
            SearchAttributePair(keyword_attr, "keyword1"),
            SearchAttributePair(
                keyword_list_attr,
                ["keywordlist1", "keywordlist2"],
            ),
            SearchAttributePair(int_attr, 123),
            SearchAttributePair(float_attr, 456.78),
            SearchAttributePair(bool_attr, True),
            SearchAttributePair(
                datetime_attr, datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
            ),
        ]
    )

    # Confirm that we can't start a workflow on existing environment
    with pytest.raises(RPCError) as err:
        await client.start_workflow(
            "some-workflow",
            id=f"wf-{uuid.uuid4()}",
            task_queue=f"tq-{uuid.uuid4()}",
            search_attributes=attrs,
        )
    assert "no mapping defined" in str(err.value)

    # But we can in a new environment with the attrs set
    async with await WorkflowEnvironment.start_local(
        search_attributes=[
            text_attr,
            keyword_attr,
            keyword_list_attr,
            int_attr,
            float_attr,
            bool_attr,
            datetime_attr,
        ]
    ) as env:
        handle = await env.client.start_workflow(
            "some-workflow",
            id=f"wf-{uuid.uuid4()}",
            task_queue=f"tq-{uuid.uuid4()}",
            search_attributes=attrs,
        )
        desc = await handle.describe()
        assert attrs == desc.typed_search_attributes


def assert_timestamp_from_now(
    ts: Union[datetime, float], expected_from_now: float, max_delta: float = 30
) -> None:
    if isinstance(ts, datetime):
        ts = ts.timestamp()
    from_now = abs(datetime.now(timezone.utc).timestamp() - ts)
    assert (expected_from_now - max_delta) < from_now < (expected_from_now + max_delta)
