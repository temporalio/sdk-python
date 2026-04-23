"""E2E integration tests for temporalio.contrib.pubsub."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from typing import Any

from dataclasses import dataclass

import nexusrpc
import nexusrpc.handler

from temporalio import activity, nexus, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pubsub import (
    PollInput,
    PollResult,
    PubSubClient,
    PubSubItem,
    PubSubMixin,
    PubSubState,
    PublishEntry,
    PublishInput,
)
from temporalio.contrib.pubsub._types import encode_data
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually, assert_task_fail_eventually, new_worker
from tests.helpers.nexus import make_nexus_endpoint_name


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
    # Long batch_interval AND long post-publish hold ensure that only a
    # working priority wakeup can deliver items before __aexit__ flushes.
    # The hold is deliberately much longer than the test's collect timeout
    # so a regression (priority no-op) surfaces as a missing item rather
    # than flaking on slow CI.
    client = PubSubClient.create(batch_interval=60.0)
    async with client:
        client.publish("events", b"normal-0")
        client.publish("events", b"normal-1")
        client.publish("events", b"priority", priority=True)
        for _ in range(100):
            activity.heartbeat()
            await asyncio.sleep(0.1)


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


async def _is_different_run(
    old_handle: WorkflowHandle[Any, Any],
    new_handle: WorkflowHandle[Any, Any],
) -> bool:
    """Check if new_handle points to a different run than old_handle."""
    try:
        desc = await new_handle.describe()
        return desc.run_id != old_handle.result_run_id
    except Exception:
        return False


async def collect_items(
    handle: WorkflowHandle[Any, Any],
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
async def test_subscribe_from_offset_and_per_item_offsets(client: Client) -> None:
    """Subscribe from zero and non-zero offsets; each item carries its global offset."""
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

        # Subscribe from offset 0 — all items, offsets 0..count-1
        all_items = await collect_items(handle, None, 0, count)
        assert len(all_items) == count
        for i, item in enumerate(all_items):
            assert item.offset == i
            assert item.data == f"item-{i}".encode()

        # Subscribe from offset 3 — items 3, 4 with offsets 3, 4
        later_items = await collect_items(handle, None, 3, 2)
        assert len(later_items) == 2
        assert later_items[0].offset == 3
        assert later_items[0].data == b"item-3"
        assert later_items[1].offset == 4
        assert later_items[1].data == b"item-4"

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
async def test_poll_truncated_offset_returns_application_error(client: Client) -> None:
    """Polling a truncated offset raises ApplicationError (not ValueError)
    and does not crash the workflow task."""
    async with new_worker(
        client,
        TruncateWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateWorkflow.run,
            id=f"pubsub-trunc-error-{uuid.uuid4()}",
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

        # Truncate up to offset 3 via update — completion is explicit.
        await handle.execute_update("truncate", 3)

        # Poll from offset 1 (truncated) — should get ApplicationError,
        # NOT crash the workflow task.
        from temporalio.client import WorkflowUpdateFailedError
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                "__pubsub_poll",
                PollInput(topics=[], from_offset=1),
                result_type=PollResult,
            )

        # Workflow should still be usable — poll from valid offset 3
        items = await collect_items(handle, None, 3, 2)
        assert len(items) == 2
        assert items[0].offset == 3

        await handle.signal("close")


@pytest.mark.asyncio
async def test_subscribe_recovers_from_truncation(client: Client) -> None:
    """subscribe() auto-recovers when offset falls behind truncation."""
    async with new_worker(
        client,
        TruncateWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateWorkflow.run,
            id=f"pubsub-trunc-recover-{uuid.uuid4()}",
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

        # Truncate first 3. The update returns after the handler completes.
        await handle.execute_update("truncate", 3)

        # subscribe from offset 1 (truncated) — should auto-recover
        # and deliver items from base_offset (3)
        pubsub = PubSubClient(handle)
        items: list[PubSubItem] = []
        try:
            async with asyncio.timeout(5):
                async for item in pubsub.subscribe(
                    from_offset=1, poll_cooldown=0
                ):
                    items.append(item)
                    if len(items) >= 2:
                        break
        except asyncio.TimeoutError:
            pass
        assert len(items) == 2
        assert items[0].offset == 3

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

        # If priority works, items arrive within milliseconds of the publish.
        # The activity holds for ~10s after priority publish; this timeout
        # gives plenty of margin for workflow/worker scheduling on slow CI
        # while staying well below the activity hold so a regression (no
        # priority wakeup) surfaces as a missing item, not a pass via
        # __aexit__ flush.
        items = await collect_items(handle, None, 0, 3, timeout=5.0)
        assert len(items) == 3
        assert items[2].data == b"priority"

        await handle.signal(PriorityWorkflow.close)


@pytest.mark.asyncio
async def test_iterator_cancellation(client: Client) -> None:
    """Cancelling a subscription iterator after it has yielded an item
    completes cleanly."""
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-cancel-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Seed one item so the iterator provably reaches an active state
        # before we cancel — no sleep-based wait.
        await handle.signal(
            "__pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=encode_data(b"seed"))]
            ),
        )

        pubsub_client = PubSubClient(handle)
        first_item = asyncio.Event()
        items: list[PubSubItem] = []

        async def subscribe_and_collect() -> None:
            async for item in pubsub_client.subscribe(
                from_offset=0, poll_cooldown=0
            ):
                items.append(item)
                first_item.set()

        task = asyncio.create_task(subscribe_and_collect())
        # Bounded wait so a subscribe regression fails fast instead of hanging.
        async with asyncio.timeout(5):
            await first_item.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(items) == 1
        assert items[0].data == b"seed"

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
async def test_flush_retry_preserves_items_after_failures(
    client: Client,
) -> None:
    """After flush failures, a subsequent successful flush delivers all items
    in publish order, exactly once.

    Exercises the retry code path behaviorally: simulated delivery failures
    must not drop items, must not duplicate them on retry, and must not
    reorder items published during the failed state.
    """
    from unittest.mock import patch

    async with new_worker(client, BasicPubSubWorkflow) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-flush-retry-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        pubsub = PubSubClient(handle)
        real_signal = handle.signal
        fail_remaining = 2

        async def maybe_failing_signal(*args: Any, **kwargs: Any) -> Any:
            nonlocal fail_remaining
            if fail_remaining > 0:
                fail_remaining -= 1
                raise RuntimeError("simulated delivery failure")
            return await real_signal(*args, **kwargs)

        with patch.object(handle, "signal", side_effect=maybe_failing_signal):
            pubsub.publish("events", b"item-0")
            pubsub.publish("events", b"item-1")
            with pytest.raises(RuntimeError):
                await pubsub._flush()

            # Publish more during the failed state — must not overtake the
            # pending retry on eventual delivery.
            pubsub.publish("events", b"item-2")
            with pytest.raises(RuntimeError):
                await pubsub._flush()

            # Third flush succeeds, delivering the pending retry batch.
            await pubsub._flush()
            # Fourth flush delivers the buffered "item-2".
            await pubsub._flush()

        items = await collect_items(handle, None, 0, 3)
        assert [i.data for i in items] == [b"item-0", b"item-1", b"item-2"]

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_flush_raises_after_max_retry_duration(client: Client) -> None:
    """When max_retry_duration is exceeded, flush raises TimeoutError and the
    client can resume publishing without losing subsequent items."""
    from unittest.mock import patch

    async with new_worker(client, BasicPubSubWorkflow) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-retry-expiry-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Inject a controllable clock into the client module. The client's
        # retry check compares `time.monotonic() - _pending_since` against
        # `max_retry_duration`, so advancing the clock between flushes makes
        # the timeout fire deterministically regardless of wall-clock speed
        # or clock resolution.
        pubsub = PubSubClient(handle, max_retry_duration=0.1)
        real_signal = handle.signal
        fail_signals = True

        async def maybe_failing_signal(*args: Any, **kwargs: Any) -> Any:
            if fail_signals:
                raise RuntimeError("simulated failure")
            return await real_signal(*args, **kwargs)

        clock = [0.0]
        with patch(
            "temporalio.contrib.pubsub._client.time.monotonic",
            side_effect=lambda: clock[0],
        ), patch.object(handle, "signal", side_effect=maybe_failing_signal):
            pubsub.publish("events", b"lost")

            # First flush fails and enters the pending-retry state.
            with pytest.raises(RuntimeError):
                await pubsub._flush()

            # Advance the clock well past max_retry_duration.
            clock[0] = 10.0

            # Next flush raises TimeoutError — the pending batch is abandoned.
            with pytest.raises(TimeoutError, match="max_retry_duration"):
                await pubsub._flush()

            # Stop failing signals; subsequent publishes must succeed.
            fail_signals = False
            pubsub.publish("events", b"kept")
            await pubsub._flush()

        items = await collect_items(handle, None, 0, 1)
        assert len(items) == 1
        assert items[0].data == b"kept"

        await handle.signal(BasicPubSubWorkflow.close)


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

        # Should have 2 items, not 3 (collect_items' update call acts as barrier)
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
        TruncateWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateWorkflow.run,
            id=f"pubsub-truncate-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 5 items via signal. collect_items below uses an update,
        # which acts as a signal barrier.
        await handle.signal(
            "__pubsub_publish",
            PublishInput(items=[
                PublishEntry(topic="events", data=encode_data(f"item-{i}".encode()))
                for i in range(5)
            ]),
        )

        # Verify all 5 items
        items = await collect_items(handle, None, 0, 5)
        assert len(items) == 5

        # Truncate up to offset 3 (discard items 0, 1, 2). The update
        # returns after the handler completes.
        await handle.execute_update("truncate", 3)

        # Offset should still be 5 (truncation moves base_offset, not tail)
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

        # Query state with a very long TTL — both publishers retained
        # (the query itself serves as a barrier for the prior signals)
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
class TruncateWorkflow(PubSubMixin):
    """Test scaffolding that exposes truncate_pubsub via a user-authored
    update.

    The contrib module does not define a built-in external truncate API —
    truncation is a workflow-internal decision (typically driven by
    consumer progress or a retention policy). Workflows that want external
    control wire up their own signal or update. We use an update here so
    callers get explicit completion (signals are fire-and-forget).
    """

    @workflow.init
    def __init__(self) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.update
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

        # The new run's workflow task must fail during init_pubsub because
        # the Any-typed field arrives as a dict, not a PubSubState. Assert
        # the specific failure instead of a timeout-based absence check.
        await assert_task_fail_eventually(new_handle)


@pytest.mark.asyncio
async def test_continue_as_new_properly_typed(client: Client) -> None:
    """CAN with PubSubState-typed pubsub_state field."""
    await _run_can_test(client, ContinueAsNewTypedWorkflow, CANWorkflowInputTyped)


# ---------------------------------------------------------------------------
# Cross-workflow pub/sub (Scenario 1)
# ---------------------------------------------------------------------------


@dataclass
class CrossWorkflowInput:
    broker_workflow_id: str
    expected_count: int


@workflow.defn
class BrokerWorkflow(PubSubMixin):
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
            self.publish("events", f"broker-{i}".encode())
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class SubscriberWorkflow:
    @workflow.run
    async def run(self, input: CrossWorkflowInput) -> list[str]:
        return await workflow.execute_activity(
            "subscribe_to_broker",
            input,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )


@activity.defn(name="subscribe_to_broker")
async def subscribe_to_broker(input: CrossWorkflowInput) -> list[str]:
    client = PubSubClient.create(
        client=activity.client(),
        workflow_id=input.broker_workflow_id,
    )
    items: list[str] = []
    async with asyncio.timeout(15.0):
        async for item in client.subscribe(
            topics=["events"], from_offset=0, poll_cooldown=0
        ):
            items.append(item.data.decode())
            activity.heartbeat()
            if len(items) >= input.expected_count:
                break
    return items


@pytest.mark.asyncio
async def test_cross_workflow_pubsub(client: Client) -> None:
    """Workflow B's activity subscribes to events published by Workflow A."""
    count = 5
    task_queue = str(uuid.uuid4())

    async with new_worker(
        client,
        BrokerWorkflow,
        SubscriberWorkflow,
        activities=[subscribe_to_broker],
        task_queue=task_queue,
    ):
        broker_id = f"pubsub-broker-{uuid.uuid4()}"
        broker_handle = await client.start_workflow(
            BrokerWorkflow.run,
            count,
            id=broker_id,
            task_queue=task_queue,
        )

        sub_handle = await client.start_workflow(
            SubscriberWorkflow.run,
            CrossWorkflowInput(
                broker_workflow_id=broker_id,
                expected_count=count,
            ),
            id=f"pubsub-subscriber-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        result = await sub_handle.result()
        assert result == [f"broker-{i}" for i in range(count)]

        # Also verify external subscription still works
        external_items = await collect_items(broker_handle, ["events"], 0, count)
        assert len(external_items) == count

        await broker_handle.signal(BrokerWorkflow.close)


# ---------------------------------------------------------------------------
# Cross-namespace pub/sub via Nexus (Scenario 2)
# ---------------------------------------------------------------------------


@dataclass
class StartBrokerInput:
    count: int
    broker_id: str


@dataclass
class NexusCallerInput:
    count: int
    broker_id: str
    endpoint: str


@workflow.defn
class NexusBrokerWorkflow(PubSubMixin):
    @workflow.init
    def __init__(self, count: int) -> None:
        self.init_pubsub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> str:
        for i in range(count):
            self.publish("events", f"nexus-{i}".encode())
        await workflow.wait_condition(lambda: self._closed)
        return "done"


@nexusrpc.service
class PubSubNexusService:
    start_broker: nexusrpc.Operation[StartBrokerInput, str]


@nexusrpc.handler.service_handler(service=PubSubNexusService)
class PubSubNexusHandler:
    @workflow_run_operation
    async def start_broker(
        self, ctx: WorkflowRunOperationContext, input: StartBrokerInput
    ) -> nexus.WorkflowHandle[str]:
        return await ctx.start_workflow(
            NexusBrokerWorkflow.run,
            input.count,
            id=input.broker_id,
        )


@workflow.defn
class NexusCallerWorkflow:
    @workflow.run
    async def run(self, input: NexusCallerInput) -> str:
        nc = workflow.create_nexus_client(
            service=PubSubNexusService,
            endpoint=input.endpoint,
        )
        return await nc.execute_operation(
            PubSubNexusService.start_broker,
            StartBrokerInput(count=input.count, broker_id=input.broker_id),
        )


async def create_cross_namespace_endpoint(
    client: Client,
    endpoint_name: str,
    target_namespace: str,
    task_queue: str,
) -> None:
    import temporalio.api.nexus.v1
    import temporalio.api.operatorservice.v1

    await client.operator_service.create_nexus_endpoint(
        temporalio.api.operatorservice.v1.CreateNexusEndpointRequest(
            spec=temporalio.api.nexus.v1.EndpointSpec(
                name=endpoint_name,
                target=temporalio.api.nexus.v1.EndpointTarget(
                    worker=temporalio.api.nexus.v1.EndpointTarget.Worker(
                        namespace=target_namespace,
                        task_queue=task_queue,
                    )
                ),
            )
        )
    )


@pytest.mark.asyncio
async def test_poll_more_ready_when_response_exceeds_size_limit(
    client: Client,
) -> None:
    """Poll response sets more_ready=True when items exceed ~1MB wire size."""
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-more-ready-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish items that total well over 1MB in the poll response.
        # Send in separate signals to stay under the RPC size limit.
        # Each item is ~200KB; 8 items = ~1.6MB wire (base64 inflates ~33%).
        chunk = b"x" * 200_000
        for _ in range(8):
            await handle.signal(
                "__pubsub_publish",
                PublishInput(
                    items=[
                        PublishEntry(topic="big", data=encode_data(chunk))
                    ]
                ),
            )

        # First poll from offset 0 — should get some items but not all.
        # (The update acts as a barrier for all prior publish signals.)
        result1: PollResult = await handle.execute_update(
            "__pubsub_poll",
            PollInput(topics=[], from_offset=0),
            result_type=PollResult,
        )
        assert result1.more_ready is True
        assert len(result1.items) < 8
        assert result1.next_offset < 8

        # Continue polling until we have all items
        all_items = list(result1.items)
        offset = result1.next_offset
        last_result: PollResult = result1
        while len(all_items) < 8:
            last_result = await handle.execute_update(
                "__pubsub_poll",
                PollInput(topics=[], from_offset=offset),
                result_type=PollResult,
            )
            all_items.extend(last_result.items)
            offset = last_result.next_offset
        assert len(all_items) == 8
        # The final poll that drained the log should set more_ready=False
        assert last_result.more_ready is False

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_subscribe_iterates_through_more_ready(client: Client) -> None:
    """Subscriber correctly yields all items when polls are size-truncated."""
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-more-ready-iter-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 8 x 200KB items (~2MB+ wire, exceeds 1MB cap)
        chunk = b"x" * 200_000
        for _ in range(8):
            await handle.signal(
                "__pubsub_publish",
                PublishInput(
                    items=[
                        PublishEntry(topic="big", data=encode_data(chunk))
                    ]
                ),
            )

        # subscribe() should seamlessly iterate through all 8 items
        items = await collect_items(handle, None, 0, 8, timeout=10.0)
        assert len(items) == 8
        for item in items:
            assert item.data == chunk

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_cross_namespace_nexus_pubsub(
    client: Client, env: WorkflowEnvironment
) -> None:
    """Nexus operation starts a pub/sub broker in another namespace; test subscribes."""
    if env.supports_time_skipping:
        pytest.skip("Nexus not supported with time-skipping server")

    count = 5
    handler_ns = f"handler-ns-{uuid.uuid4().hex[:8]}"
    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    broker_id = f"nexus-broker-{uuid.uuid4()}"

    # Register the handler namespace with the dev server
    import google.protobuf.duration_pb2
    import temporalio.api.workflowservice.v1

    await client.workflow_service.register_namespace(
        temporalio.api.workflowservice.v1.RegisterNamespaceRequest(
            namespace=handler_ns,
            workflow_execution_retention_period=google.protobuf.duration_pb2.Duration(
                seconds=86400,
            ),
        )
    )

    handler_client = await Client.connect(
        client.service_client.config.target_host,
        namespace=handler_ns,
    )

    # Create endpoint targeting the handler namespace
    await create_cross_namespace_endpoint(
        client,
        endpoint_name,
        target_namespace=handler_ns,
        task_queue=task_queue,
    )

    # Handler worker in handler namespace
    async with Worker(
        handler_client,
        task_queue=task_queue,
        workflows=[NexusBrokerWorkflow],
        nexus_service_handlers=[PubSubNexusHandler()],
    ):
        # Caller worker in default namespace
        caller_tq = str(uuid.uuid4())
        async with new_worker(
            client,
            NexusCallerWorkflow,
            task_queue=caller_tq,
        ):
            # Start caller — invokes Nexus op which starts broker in handler ns
            caller_handle = await client.start_workflow(
                NexusCallerWorkflow.run,
                NexusCallerInput(
                    count=count,
                    broker_id=broker_id,
                    endpoint=endpoint_name,
                ),
                id=f"nexus-caller-{uuid.uuid4()}",
                task_queue=caller_tq,
            )

            # Wait for the broker workflow to be started by the Nexus operation
            broker_handle = handler_client.get_workflow_handle(broker_id)

            async def broker_started() -> bool:
                try:
                    await broker_handle.describe()
                    return True
                except Exception:
                    return False

            await assert_eq_eventually(
                True, broker_started, timeout=timedelta(seconds=15)
            )

            # Subscribe to broker events from the handler namespace
            items = await collect_items(broker_handle, ["events"], 0, count)
            assert len(items) == count
            for i in range(count):
                assert items[i].topic == "events"
                assert items[i].data == f"nexus-{i}".encode()

            # Clean up — signal broker to close so caller can complete
            await broker_handle.signal("close")
            result = await caller_handle.result()
            assert result == "done"
