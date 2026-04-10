"""E2E integration tests for temporalio.contrib.pubsub."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from typing import Any

from dataclasses import dataclass

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pubsub import (
    PubSubClient,
    PubSubItem,
    PubSubMixin,
    PubSubState,
    PublishEntry,
    PublishInput,
)
from temporalio.contrib.pubsub._types import encode_data
from tests.helpers import assert_eq_eventually, new_worker


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
class MaxBatchWorkflow(PubSubMixin):
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
            "publish_with_max_batch",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        self.publish("status", b"activity_done")
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
    client = PubSubClient.create(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


@activity.defn(name="publish_multi_topic")
async def publish_multi_topic(count: int) -> None:
    topics = ["a", "b", "c"]
    client = PubSubClient.create(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            topic = topics[i % len(topics)]
            client.publish(topic, f"{topic}-{i}".encode())


@activity.defn(name="publish_with_priority")
async def publish_with_priority() -> None:
    client = PubSubClient.create(batch_interval=60.0)
    async with client:
        client.publish("events", b"normal-0")
        client.publish("events", b"normal-1")
        client.publish("events", b"priority", priority=True)
        # Give the flusher time to wake and flush
        await asyncio.sleep(0.5)


@activity.defn(name="publish_batch_test")
async def publish_batch_test(count: int) -> None:
    client = PubSubClient.create(batch_interval=60.0)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


@activity.defn(name="publish_with_max_batch")
async def publish_with_max_batch(count: int) -> None:
    client = PubSubClient.create(batch_interval=60.0, max_batch_size=3)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())
        # Long batch_interval ensures only max_batch_size triggers flushes
        # Context manager exit flushes any remainder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _is_different_run(old_handle, new_handle) -> bool:
    """Check if new_handle points to a different run than old_handle."""
    try:
        desc = await new_handle.describe()
        return desc.run_id != old_handle.result_run_id
    except Exception:
        return False


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
            async for item in client.subscribe(
                topics=topics, from_offset=from_offset, poll_cooldown=0
            ):
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
async def test_per_item_offsets(client: Client) -> None:
    """Each yielded PubSubItem carries its correct global offset."""
    count = 5
    async with new_worker(
        client,
        WorkflowSidePublishWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            WorkflowSidePublishWorkflow.run,
            count,
            id=f"pubsub-item-offset-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        items = await collect_items(handle, None, 0, count)
        assert len(items) == count
        for i, item in enumerate(items):
            assert item.offset == i, f"item {i} has offset {item.offset}"

        # Subscribe from offset 3 — offsets should be 3, 4
        later_items = await collect_items(handle, None, 3, 2)
        assert len(later_items) == 2
        assert later_items[0].offset == 3
        assert later_items[1].offset == 4

        await handle.signal(WorkflowSidePublishWorkflow.close)


@pytest.mark.asyncio
async def test_per_item_offsets_with_topic_filter(client: Client) -> None:
    """Per-item offsets are global (not per-topic) even when filtering."""
    count = 9  # 3 per topic (a, b, c round-robin)
    async with new_worker(
        client,
        MultiTopicWorkflow,
        activities=[publish_multi_topic],
    ) as worker:
        handle = await client.start_workflow(
            MultiTopicWorkflow.run,
            count,
            id=f"pubsub-item-offset-filter-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Subscribe to topic "a" only — items are at global offsets 0, 3, 6
        a_items = await collect_items(handle, ["a"], 0, 3)
        assert len(a_items) == 3
        assert a_items[0].offset == 0
        assert a_items[1].offset == 3
        assert a_items[2].offset == 6

        # Subscribe to topic "b" — items are at global offsets 1, 4, 7
        b_items = await collect_items(handle, ["b"], 0, 3)
        assert len(b_items) == 3
        assert b_items[0].offset == 1
        assert b_items[1].offset == 4
        assert b_items[2].offset == 7

        await handle.signal(MultiTopicWorkflow.close)


@pytest.mark.asyncio
async def test_per_item_offsets_after_truncation(client: Client) -> None:
    """Per-item offsets remain correct after log truncation."""
    async with new_worker(
        client,
        TruncateSignalWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateSignalWorkflow.run,
            id=f"pubsub-item-offset-trunc-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 5 items
        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[
                PublishEntry(topic="events", data=encode_data(f"item-{i}".encode()))
                for i in range(5)
            ]),
        )
        await asyncio.sleep(0.5)

        # Truncate up to offset 3
        await handle.signal("truncate", 3)
        await asyncio.sleep(0.3)

        # Items 3, 4 should have offsets 3, 4
        items = await collect_items(handle, None, 3, 2)
        assert len(items) == 2
        assert items[0].offset == 3
        assert items[1].offset == 4

        await handle.signal("close")


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
            async for item in pubsub_client.subscribe(
                from_offset=0, poll_cooldown=0
            ):
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
            PublishInput(items=[PublishEntry(topic="events", data=encode_data(b"test-item"))]),
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


@pytest.mark.asyncio
async def test_max_batch_size(client: Client) -> None:
    """max_batch_size triggers auto-flush without waiting for timer."""
    count = 7  # with max_batch_size=3: flushes at 3, 6, then remainder 1 on exit
    async with new_worker(
        client,
        MaxBatchWorkflow,
        activities=[publish_with_max_batch],
        max_cached_workflows=0,
    ) as worker:
        handle = await client.start_workflow(
            MaxBatchWorkflow.run,
            count,
            id=f"pubsub-maxbatch-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # count items from activity + 1 "activity_done" from workflow
        items = await collect_items(handle, None, 0, count + 1, timeout=15.0)
        assert len(items) == count + 1
        for i in range(count):
            assert items[i].data == f"item-{i}".encode()
        await handle.signal(MaxBatchWorkflow.close)


@pytest.mark.asyncio
async def test_replay_safety(client: Client) -> None:
    """Pub/sub mixin survives workflow replay (max_cached_workflows=0)."""
    async with new_worker(
        client,
        InterleavedWorkflow,
        activities=[publish_items],
        max_cached_workflows=0,
    ) as worker:
        handle = await client.start_workflow(
            InterleavedWorkflow.run,
            5,
            id=f"pubsub-replay-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # 1 (started) + 5 (activity) + 1 (done) = 7
        items = await collect_items(handle, None, 0, 7)
        assert len(items) == 7
        assert items[0].data == b"started"
        assert items[6].data == b"done"
        await handle.signal(InterleavedWorkflow.close)


@pytest.mark.asyncio
async def test_flush_keeps_pending_on_signal_failure(client: Client) -> None:
    """If flush signal fails, items stay in _pending for retry with same sequence.

    This matches the TLA+-verified algorithm (PubSubDedup.tla): on failure,
    the pending batch and sequence are kept so the next _flush() retries with
    the SAME sequence number. The confirmed sequence (_sequence) does NOT
    advance until delivery is confirmed.
    """
    bogus_handle = client.get_workflow_handle("nonexistent-workflow-id")
    pubsub = PubSubClient(bogus_handle)

    pubsub.publish("events", b"item-0")
    pubsub.publish("events", b"item-1")
    assert len(pubsub._buffer) == 2

    # flush should fail (workflow doesn't exist)
    with pytest.raises(Exception):
        await pubsub._flush()

    # Items moved to _pending (not restored to _buffer)
    assert len(pubsub._buffer) == 0
    assert pubsub._pending is not None
    assert len(pubsub._pending) == 2
    assert pubsub._pending[0].data == encode_data(b"item-0")
    assert pubsub._pending[1].data == encode_data(b"item-1")
    # Pending sequence is set, confirmed sequence is NOT advanced
    assert pubsub._pending_seq == 1
    assert pubsub._sequence == 0

    # New items published during failure go to _buffer (not _pending)
    pubsub.publish("events", b"item-2")
    assert len(pubsub._buffer) == 1
    assert pubsub._pending is not None  # Still set for retry

    # Next flush retries the pending batch with the same sequence
    with pytest.raises(Exception):
        await pubsub._flush()
    assert pubsub._pending_seq == 1  # Same sequence on retry
    assert pubsub._sequence == 0  # Still not advanced


@pytest.mark.asyncio
async def test_max_retry_duration_expiry(client: Client) -> None:
    """Flush raises TimeoutError when max_retry_duration is exceeded."""
    bogus_handle = client.get_workflow_handle("nonexistent-workflow-id")
    pubsub = PubSubClient(bogus_handle, max_retry_duration=0.1)

    pubsub.publish("events", b"item-0")

    # First flush fails, sets pending
    with pytest.raises(Exception, match="not found"):
        await pubsub._flush()
    assert pubsub._pending is not None

    # Wait for retry duration to expire
    await asyncio.sleep(0.2)

    # Next flush should raise TimeoutError and clear pending
    with pytest.raises(TimeoutError, match="max_retry_duration"):
        await pubsub._flush()
    assert pubsub._pending is None
    assert pubsub._sequence == 0


@pytest.mark.asyncio
async def test_dedup_rejects_duplicate_signal(client: Client) -> None:
    """Workflow deduplicates signals with the same publisher_id + sequence."""
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-dedup-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Send a batch with publisher_id and sequence
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"item-0"))],
                publisher_id="test-pub",
                sequence=1,
            ),
        )

        # Send the same sequence again — should be deduped
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"duplicate"))],
                publisher_id="test-pub",
                sequence=1,
            ),
        )

        # Send a new sequence — should go through
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"item-1"))],
                publisher_id="test-pub",
                sequence=2,
            ),
        )

        await asyncio.sleep(0.5)

        # Should have 2 items, not 3
        items = await collect_items(handle, None, 0, 2)
        assert len(items) == 2
        assert items[0].data == b"item-0"
        assert items[1].data == b"item-1"

        # Verify offset is 2 (not 3)
        pubsub_client = PubSubClient(handle)
        offset = await pubsub_client.get_offset()
        assert offset == 2

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_truncate_pubsub(client: Client) -> None:
    """truncate_pubsub discards prefix and adjusts base_offset."""
    async with new_worker(
        client,
        TruncateSignalWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateSignalWorkflow.run,
            id=f"pubsub-truncate-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 5 items via signal
        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[
                PublishEntry(topic="events", data=encode_data(f"item-{i}".encode()))
                for i in range(5)
            ]),
        )
        await asyncio.sleep(0.5)

        # Verify all 5 items
        items = await collect_items(handle, None, 0, 5)
        assert len(items) == 5

        # Truncate up to offset 3 (discard items 0, 1, 2)
        await handle.signal("truncate", 3)
        await asyncio.sleep(0.3)

        # Offset should still be 5
        pubsub_client = PubSubClient(handle)
        offset = await pubsub_client.get_offset()
        assert offset == 5

        # Reading from offset 3 should work (items 3, 4)
        items_after = await collect_items(handle, None, 3, 2)
        assert len(items_after) == 2
        assert items_after[0].data == b"item-3"
        assert items_after[1].data == b"item-4"

        await handle.signal("close")


@pytest.mark.asyncio
async def test_ttl_pruning_in_get_pubsub_state(client: Client) -> None:
    """get_pubsub_state prunes stale publisher entries based on TTL."""
    async with new_worker(
        client,
        TTLTestWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TTLTestWorkflow.run,
            id=f"pubsub-ttl-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish from two different publishers
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"from-a"))],
                publisher_id="pub-a",
                sequence=1,
            ),
        )
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"from-b"))],
                publisher_id="pub-b",
                sequence=1,
            ),
        )
        await asyncio.sleep(0.5)

        # Query state with a very long TTL — both publishers retained
        state = await handle.query(TTLTestWorkflow.get_state_with_ttl, 9999.0)
        assert "pub-a" in state.publisher_sequences
        assert "pub-b" in state.publisher_sequences

        # Query state with TTL=0 — both publishers pruned
        state_pruned = await handle.query(TTLTestWorkflow.get_state_with_ttl, 0.0)
        assert "pub-a" not in state_pruned.publisher_sequences
        assert "pub-b" not in state_pruned.publisher_sequences

        # Items are still in the log regardless of pruning
        assert len(state_pruned.log) == 2

        await handle.signal("close")


# ---------------------------------------------------------------------------
# Truncate and TTL test workflows
# ---------------------------------------------------------------------------


@workflow.defn
class TruncateSignalWorkflow(PubSubMixin):
    """Workflow that accepts a truncate signal for testing."""

    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.signal
    def truncate(self, up_to_offset: int) -> None:
        self.truncate_pubsub(up_to_offset)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class TTLTestWorkflow(PubSubMixin):
    """Workflow that exposes get_pubsub_state via query for TTL testing."""

    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def get_state_with_ttl(self, ttl: float) -> PubSubState:
        return self.get_pubsub_state(publisher_ttl=ttl)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


# ---------------------------------------------------------------------------
# Continue-as-new workflow and test
# ---------------------------------------------------------------------------


@dataclass
class CANWorkflowInputAny:
    """Uses Any typing — reproduces the pitfall."""
    pubsub_state: Any = None


@dataclass
class CANWorkflowInputTyped:
    """Uses proper typing."""
    pubsub_state: PubSubState | None = None


@workflow.defn
class ContinueAsNewAnyWorkflow(PubSubMixin):
    """CAN workflow using Any-typed pubsub_state (reproduces samples pattern)."""

    @workflow.init
    def __init__(self, input: CANWorkflowInputAny) -> None:
        self.init_pubsub(prior_state=input.pubsub_state)
        self._should_continue = False
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.signal
    def trigger_continue(self) -> None:
        self._should_continue = True

    @workflow.run
    async def run(self, input: CANWorkflowInputAny) -> None:
        while True:
            await workflow.wait_condition(
                lambda: self._should_continue or self._closed
            )
            if self._closed:
                return
            if self._should_continue:
                self._should_continue = False
                self.drain_pubsub()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(args=[CANWorkflowInputAny(
                    pubsub_state=self.get_pubsub_state(),
                )])


@workflow.defn
class ContinueAsNewTypedWorkflow(PubSubMixin):
    """CAN workflow using properly-typed pubsub_state."""

    @workflow.init
    def __init__(self, input: CANWorkflowInputTyped) -> None:
        self.init_pubsub(prior_state=input.pubsub_state)
        self._should_continue = False
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.signal
    def trigger_continue(self) -> None:
        self._should_continue = True

    @workflow.run
    async def run(self, input: CANWorkflowInputTyped) -> None:
        while True:
            await workflow.wait_condition(
                lambda: self._should_continue or self._closed
            )
            if self._closed:
                return
            if self._should_continue:
                self._should_continue = False
                self.drain_pubsub()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(args=[CANWorkflowInputTyped(
                    pubsub_state=self.get_pubsub_state(),
                )])


async def _run_can_test(can_client: Client, workflow_cls, input_cls) -> None:
    """Shared CAN test logic: publish, CAN, verify items survive."""
    async with new_worker(
        can_client,
        workflow_cls,
    ) as worker:
        handle = await can_client.start_workflow(
            workflow_cls.run,
            input_cls(),
            id=f"pubsub-can-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 3 items via signal
        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[
                PublishEntry(topic="events", data=encode_data(b"item-0")),
                PublishEntry(topic="events", data=encode_data(b"item-1")),
                PublishEntry(topic="events", data=encode_data(b"item-2")),
            ]),
        )

        # Verify items are there
        items_before = await collect_items(handle, None, 0, 3)
        assert len(items_before) == 3

        # Trigger continue-as-new
        await handle.signal(workflow_cls.trigger_continue)

        # Wait for new run to start (poll, don't sleep)
        new_handle = can_client.get_workflow_handle(handle.id)
        await assert_eq_eventually(
            True,
            lambda: _is_different_run(handle, new_handle),
        )

        # The 3 items from before CAN should still be readable
        items_after = await collect_items(new_handle, None, 0, 3)
        assert len(items_after) == 3
        assert items_after[0].data == b"item-0"
        assert items_after[1].data == b"item-1"
        assert items_after[2].data == b"item-2"

        # New items should get offset 3+
        await new_handle.signal(
            "__pubsub_publish",
            PublishInput(items=[PublishEntry(topic="events", data=encode_data(b"item-3"))]),
        )
        items_all = await collect_items(new_handle, None, 0, 4)
        assert len(items_all) == 4
        assert items_all[3].data == b"item-3"

        await new_handle.signal(workflow_cls.close)


@pytest.mark.asyncio
async def test_continue_as_new_any_typed_fails(client: Client) -> None:
    """Any-typed pubsub_state does NOT survive CAN — documents the pitfall.

    The default data converter deserializes Any fields as plain dicts, losing
    the PubSubState type. Use ``PubSubState | None`` instead.
    """
    async with new_worker(
        client,
        ContinueAsNewAnyWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            ContinueAsNewAnyWorkflow.run,
            CANWorkflowInputAny(),
            id=f"pubsub-can-any-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[PublishEntry(topic="events", data=encode_data(b"item-0"))]),
        )
        items = await collect_items(handle, None, 0, 1)
        assert len(items) == 1

        # Trigger CAN — the new run will fail to deserialize pubsub_state
        await handle.signal(ContinueAsNewAnyWorkflow.trigger_continue)

        # Wait for CAN to happen
        new_handle = client.get_workflow_handle(handle.id)
        await assert_eq_eventually(
            True,
            lambda: _is_different_run(handle, new_handle),
        )

        # The new run should be broken — items are NOT accessible
        items_after = await collect_items(new_handle, None, 0, 1, timeout=3.0)
        assert len(items_after) == 0  # fails because workflow can't start


@pytest.mark.asyncio
async def test_continue_as_new_properly_typed(client: Client) -> None:
    """CAN with PubSubState-typed pubsub_state field."""
    await _run_can_test(client, ContinueAsNewTypedWorkflow, CANWorkflowInputTyped)
