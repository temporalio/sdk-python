"""E2E integration tests for temporalio.contrib.pubsub."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pubsub import (
    PollInput,
    PollResult,
    PubSubClient,
    PubSubItem,
    PubSubMixin,
    PublishEntry,
    PublishInput,
    activity_pubsub_client,
)
from tests.helpers import new_worker


# ---------------------------------------------------------------------------
# Test workflows (must be module-level, not local classes)
# ---------------------------------------------------------------------------


@workflow.defn
class BasicPubSubWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class ActivityPublishWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        await workflow.execute_activity(
            "publish_items",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        self.publish("status", b"activity_done")
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class WorkflowSidePublishWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        for i in range(count):
            self.publish("events", f"item-{i}".encode())
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class MultiTopicWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        await workflow.execute_activity(
            "publish_multi_topic",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class InterleavedWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        self.publish("status", b"started")
        await workflow.execute_activity(
            "publish_items",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        self.publish("status", b"done")
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class PriorityWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            "publish_with_priority",
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class FlushOnExitWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        await workflow.execute_activity(
            "publish_batch_test",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class MixinCoexistenceWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._app_data: list[str] = []
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.signal
    def app_signal(self, value: str) -> None:
        self._app_data.append(value)

    @workflow.query
    def app_query(self) -> list[str]:
        return self._app_data

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="publish_items")
async def publish_items(count: int) -> None:
    client = activity_pubsub_client(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


@activity.defn(name="publish_multi_topic")
async def publish_multi_topic(count: int) -> None:
    topics = ["a", "b", "c"]
    client = activity_pubsub_client(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            topic = topics[i % len(topics)]
            client.publish(topic, f"{topic}-{i}".encode())


@activity.defn(name="publish_with_priority")
async def publish_with_priority() -> None:
    client = activity_pubsub_client(batch_interval=60.0)
    async with client:
        client.publish("events", b"normal-0")
        client.publish("events", b"normal-1")
        client.publish("events", b"priority", priority=True)
        # Give the flusher time to wake and flush
        await asyncio.sleep(0.5)


@activity.defn(name="publish_batch_test")
async def publish_batch_test(count: int) -> None:
    client = activity_pubsub_client(batch_interval=60.0)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_items(
    handle,
    topics: list[str] | None,
    from_offset: int,
    expected_count: int,
    timeout: float = 15.0,
) -> list[PubSubItem]:
    """Subscribe and collect exactly expected_count items, with timeout."""
    client = PubSubClient(handle)
    items: list[PubSubItem] = []
    try:
        async with asyncio.timeout(timeout):
            async for item in client.subscribe(topics=topics, from_offset=from_offset):
                items.append(item)
                if len(items) >= expected_count:
                    break
    except asyncio.TimeoutError:
        pass
    return items


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_publish_and_subscribe(client: Client) -> None:
    """Activity publishes items, external client subscribes and receives them."""
    count = 10
    async with new_worker(
        client,
        ActivityPublishWorkflow,
        activities=[publish_items],
    ) as worker:
        handle = await client.start_workflow(
            ActivityPublishWorkflow.run,
            count,
            id=f"pubsub-basic-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Collect activity items + the "activity_done" status item
        items = await collect_items(handle, None, 0, count + 1)
        assert len(items) == count + 1

        # Check activity items
        for i in range(count):
            assert items[i].topic == "events"
            assert items[i].data == f"item-{i}".encode()
            assert items[i].offset == i

        # Check workflow-side status item
        assert items[count].topic == "status"
        assert items[count].data == b"activity_done"

        await handle.signal(ActivityPublishWorkflow.close)


@pytest.mark.asyncio
async def test_topic_filtering(client: Client) -> None:
    """Publish to multiple topics, subscribe with filter."""
    count = 9  # 3 per topic
    async with new_worker(
        client,
        MultiTopicWorkflow,
        activities=[publish_multi_topic],
    ) as worker:
        handle = await client.start_workflow(
            MultiTopicWorkflow.run,
            count,
            id=f"pubsub-filter-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Subscribe to topic "a" only — should get 3 items
        a_items = await collect_items(handle, ["a"], 0, 3)
        assert len(a_items) == 3
        assert all(item.topic == "a" for item in a_items)

        # Subscribe to ["a", "c"] — should get 6 items
        ac_items = await collect_items(handle, ["a", "c"], 0, 6)
        assert len(ac_items) == 6
        assert all(item.topic in ("a", "c") for item in ac_items)

        # Subscribe to all (None) — should get all 9
        all_items = await collect_items(handle, None, 0, 9)
        assert len(all_items) == 9

        await handle.signal(MultiTopicWorkflow.close)


@pytest.mark.asyncio
async def test_subscribe_from_offset(client: Client) -> None:
    """Subscribe from a non-zero offset."""
    count = 5
    async with new_worker(
        client,
        WorkflowSidePublishWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            WorkflowSidePublishWorkflow.run,
            count,
            id=f"pubsub-offset-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Subscribe from offset 3 — should get items 3, 4
        items = await collect_items(handle, None, 3, 2)
        assert len(items) == 2
        assert items[0].data == b"item-3"
        assert items[1].data == b"item-4"

        # Subscribe from offset 0 — should get all 5
        all_items = await collect_items(handle, None, 0, 5)
        assert len(all_items) == 5

        await handle.signal(WorkflowSidePublishWorkflow.close)


@pytest.mark.asyncio
async def test_workflow_and_activity_publish_interleaved(client: Client) -> None:
    """Workflow publishes status events around activity publishing."""
    count = 5
    async with new_worker(
        client,
        InterleavedWorkflow,
        activities=[publish_items],
    ) as worker:
        handle = await client.start_workflow(
            InterleavedWorkflow.run,
            count,
            id=f"pubsub-interleave-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Total: 1 (started) + count (activity) + 1 (done) = count + 2
        items = await collect_items(handle, None, 0, count + 2)
        assert len(items) == count + 2

        # First item is workflow-side "started"
        assert items[0].topic == "status"
        assert items[0].data == b"started"

        # Middle items are from activity
        for i in range(count):
            assert items[i + 1].topic == "events"
            assert items[i + 1].data == f"item-{i}".encode()

        # Last item is workflow-side "done"
        assert items[count + 1].topic == "status"
        assert items[count + 1].data == b"done"

        await handle.signal(InterleavedWorkflow.close)


@pytest.mark.asyncio
async def test_priority_flush(client: Client) -> None:
    """Priority publish triggers immediate flush without waiting for timer."""
    async with new_worker(
        client,
        PriorityWorkflow,
        activities=[publish_with_priority],
    ) as worker:
        handle = await client.start_workflow(
            PriorityWorkflow.run,
            id=f"pubsub-priority-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # If priority works, we get all 3 items quickly despite 60s batch interval
        items = await collect_items(handle, None, 0, 3, timeout=10.0)
        assert len(items) == 3
        assert items[2].data == b"priority"

        await handle.signal(PriorityWorkflow.close)


@pytest.mark.asyncio
async def test_iterator_cancellation(client: Client) -> None:
    """Cancelling a subscription iterator completes cleanly."""
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-cancel-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        pubsub_client = PubSubClient(handle)

        async def subscribe_and_collect():
            items = []
            async for item in pubsub_client.subscribe(from_offset=0):
                items.append(item)
            return items

        task = asyncio.create_task(subscribe_and_collect())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_context_manager_flushes_on_exit(client: Client) -> None:
    """Context manager exit flushes all buffered items."""
    count = 5
    async with new_worker(
        client,
        FlushOnExitWorkflow,
        activities=[publish_batch_test],
    ) as worker:
        handle = await client.start_workflow(
            FlushOnExitWorkflow.run,
            count,
            id=f"pubsub-flush-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Despite 60s batch interval, all items arrive because __aexit__ flushes
        items = await collect_items(handle, None, 0, count, timeout=15.0)
        assert len(items) == count
        for i in range(count):
            assert items[i].data == f"item-{i}".encode()

        await handle.signal(FlushOnExitWorkflow.close)


@pytest.mark.asyncio
async def test_concurrent_subscribers(client: Client) -> None:
    """Two subscribers on different topics receive correct items concurrently."""
    count = 6  # 2 per topic
    async with new_worker(
        client,
        MultiTopicWorkflow,
        activities=[publish_multi_topic],
    ) as worker:
        handle = await client.start_workflow(
            MultiTopicWorkflow.run,
            count,
            id=f"pubsub-concurrent-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        a_task = asyncio.create_task(collect_items(handle, ["a"], 0, 2))
        b_task = asyncio.create_task(collect_items(handle, ["b"], 0, 2))

        a_items, b_items = await asyncio.gather(a_task, b_task)

        assert len(a_items) == 2
        assert all(item.topic == "a" for item in a_items)
        assert len(b_items) == 2
        assert all(item.topic == "b" for item in b_items)

        await handle.signal(MultiTopicWorkflow.close)


@pytest.mark.asyncio
async def test_mixin_coexistence(client: Client) -> None:
    """PubSubMixin works alongside application signals and queries."""
    async with new_worker(
        client,
        MixinCoexistenceWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            MixinCoexistenceWorkflow.run,
            id=f"pubsub-coexist-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Use application signal
        await handle.signal(MixinCoexistenceWorkflow.app_signal, "hello")
        await handle.signal(MixinCoexistenceWorkflow.app_signal, "world")

        # Use pub/sub signal
        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[PublishEntry(topic="events", data=b"test-item")]),
        )

        # Give signals time to be processed
        await asyncio.sleep(0.5)

        # Query application state
        app_data = await handle.query(MixinCoexistenceWorkflow.app_query)
        assert app_data == ["hello", "world"]

        # Query pub/sub offset
        pubsub_client = PubSubClient(handle)
        offset = await pubsub_client.get_offset()
        assert offset == 1

        # Subscribe to pub/sub
        items = await collect_items(handle, None, 0, 1)
        assert len(items) == 1
        assert items[0].topic == "events"

        await handle.signal(MixinCoexistenceWorkflow.close)
