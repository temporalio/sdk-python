"""E2E integration tests for temporalio.contrib.pubsub."""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import patch

if sys.version_info >= (3, 11):
    from asyncio import timeout as _async_timeout  # pyright: ignore[reportUnreachable]
else:
    from async_timeout import (  # pyright: ignore[reportMissingImports, reportUnreachable]
        timeout as _async_timeout,
    )

import google.protobuf.duration_pb2
import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.api.nexus.v1
import temporalio.api.operatorservice.v1
import temporalio.api.workflowservice.v1
from temporalio import activity, nexus, workflow
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pubsub import (
    PollInput,
    PollResult,
    PublishEntry,
    PublishInput,
    PubSub,
    PubSubClient,
    PubSubItem,
    PubSubState,
)
from temporalio.contrib.pubsub._types import _encode_payload
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eq_eventually, new_worker
from tests.helpers.nexus import make_nexus_endpoint_name


def _wire_bytes(data: bytes) -> str:
    """Build a PublishEntry.data string from raw bytes.

    Mirrors what :class:`PubSubClient` produces on the encode path:
    default payload converter turns the bytes into a ``Payload``, which
    is then proto-serialized and base64-encoded for the wire.
    """
    payload = DataConverter.default.payload_converter.to_payloads([data])[0]
    return _encode_payload(payload)


# ---------------------------------------------------------------------------
# Test workflows (must be module-level, not local classes)
# ---------------------------------------------------------------------------


@workflow.defn
class BasicPubSubWorkflow:
    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class ActivityPublishWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
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
        self.pubsub.publish("status", b"activity_done")
        await workflow.wait_condition(lambda: self._closed)


@dataclass
class AgentEvent:
    kind: str
    payload: dict[str, Any]


@workflow.defn
class StructuredPublishWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        for i in range(count):
            self.pubsub.publish("events", AgentEvent(kind="tick", payload={"i": i}))
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class WorkflowSidePublishWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        for i in range(count):
            self.pubsub.publish("events", f"item-{i}".encode())
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class MultiTopicWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
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
class InterleavedWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        self.pubsub.publish("status", b"started")
        await workflow.execute_activity(
            "publish_items",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        self.pubsub.publish("status", b"done")
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class PriorityWorkflow:
    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()
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
class FlushOnExitWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
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
class MaxBatchWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def publisher_sequences(self) -> dict[str, int]:
        return dict(self.pubsub._publisher_sequences)

    @workflow.run
    async def run(self, count: int) -> None:
        await workflow.execute_activity(
            "publish_with_max_batch",
            count,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
        )
        self.pubsub.publish("status", b"activity_done")
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class LatePubSubWorkflow:
    """Calls PubSub() from @workflow.run, not from @workflow.init.

    The constructor inspects the caller's frame and requires the
    function name to be ``__init__``; called from ``run``, it must
    raise ``RuntimeError``. The workflow returns the error message so
    the test can assert on it without forcing a workflow task failure.
    """

    @workflow.run
    async def run(self) -> str:
        try:
            PubSub()
        except RuntimeError as e:
            return str(e)
        return "no error raised"


@workflow.defn
class DoubleInitWorkflow:
    """Calls PubSub() twice from @workflow.init.

    The first call succeeds; the second must raise RuntimeError because
    the pub/sub signal handler is already registered. The workflow
    stashes the error message so the test can assert on it without
    forcing a workflow task failure.
    """

    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()
        self._closed = False
        self.double_init_error: str | None = None
        try:
            PubSub()
        except RuntimeError as e:
            self.double_init_error = str(e)

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def get_double_init_error(self) -> str | None:
        return self.double_init_error

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="publish_items")
async def publish_items(count: int) -> None:
    client = PubSubClient.from_activity(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


@activity.defn(name="publish_multi_topic")
async def publish_multi_topic(count: int) -> None:
    topics = ["a", "b", "c"]
    client = PubSubClient.from_activity(batch_interval=0.5)
    async with client:
        for i in range(count):
            activity.heartbeat()
            topic = topics[i % len(topics)]
            client.publish(topic, f"{topic}-{i}".encode())


@activity.defn(name="publish_with_priority")
async def publish_with_priority() -> None:
    # Long batch_interval AND long post-publish hold ensure that only a
    # working force_flush wakeup can deliver items before __aexit__ flushes.
    # The hold is deliberately much longer than the test's collect timeout
    # so a regression (force_flush no-op) surfaces as a missing item rather
    # than flaking on slow CI.
    client = PubSubClient.from_activity(batch_interval=60.0)
    async with client:
        client.publish("events", b"normal-0")
        client.publish("events", b"normal-1")
        client.publish("events", b"priority", force_flush=True)
        for _ in range(100):
            activity.heartbeat()
            await asyncio.sleep(0.1)


@activity.defn(name="publish_batch_test")
async def publish_batch_test(count: int) -> None:
    client = PubSubClient.from_activity(batch_interval=60.0)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())


@activity.defn(name="publish_with_max_batch")
async def publish_with_max_batch(count: int) -> None:
    client = PubSubClient.from_activity(batch_interval=60.0, max_batch_size=3)
    async with client:
        for i in range(count):
            activity.heartbeat()
            client.publish("events", f"item-{i}".encode())
            # Yield so the flusher task can run when max_batch_size triggers
            # _flush_event. Real workloads (e.g. agents awaiting LLM streams)
            # yield constantly; a tight loop with no awaits would never let
            # the flusher fire and would collapse back to exit-only flushing.
            await asyncio.sleep(0)
        # Long batch_interval ensures only max_batch_size triggers flushes.
        # Context manager exit flushes any remainder.


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
    client: Client,
    handle: WorkflowHandle[Any, Any],
    topics: list[str] | None,
    from_offset: int,
    expected_count: int,
    timeout: float = 15.0,
    *,
    result_type: type | None = bytes,
) -> list[PubSubItem]:
    """Subscribe and collect exactly expected_count items, with timeout.

    Default ``result_type=bytes`` matches the bytes-oriented tests that
    compare ``item.data`` against literal byte strings. Pass
    ``result_type=None`` to receive raw ``Payload`` objects.
    """
    pubsub = PubSubClient.create(client, handle.id)
    items: list[PubSubItem] = []
    try:
        async with _async_timeout(timeout):
            async for item in pubsub.subscribe(
                topics=topics,
                from_offset=from_offset,
                poll_cooldown=0,
                result_type=result_type,
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
        items = await collect_items(client, handle, None, 0, count + 1)
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
async def test_structured_type_round_trip(client: Client) -> None:
    """Workflow publishes dataclass values; subscriber decodes via result_type."""
    count = 4
    async with new_worker(client, StructuredPublishWorkflow) as worker:
        handle = await client.start_workflow(
            StructuredPublishWorkflow.run,
            count,
            id=f"pubsub-structured-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        items = await collect_items(
            client, handle, None, 0, count, result_type=AgentEvent
        )
        assert len(items) == count
        for i, item in enumerate(items):
            assert isinstance(item.data, AgentEvent)
            assert item.data == AgentEvent(kind="tick", payload={"i": i})

        await handle.signal(StructuredPublishWorkflow.close)


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
        a_items = await collect_items(client, handle, ["a"], 0, 3)
        assert len(a_items) == 3
        assert all(item.topic == "a" for item in a_items)

        # Subscribe to ["a", "c"] — should get 6 items
        ac_items = await collect_items(client, handle, ["a", "c"], 0, 6)
        assert len(ac_items) == 6
        assert all(item.topic in ("a", "c") for item in ac_items)

        # Subscribe to all (None) — should get all 9
        all_items = await collect_items(client, handle, None, 0, 9)
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
        all_items = await collect_items(client, handle, None, 0, count)
        assert len(all_items) == count
        for i, item in enumerate(all_items):
            assert item.offset == i
            assert item.data == f"item-{i}".encode()

        # Subscribe from offset 3 — items 3, 4 with offsets 3, 4
        later_items = await collect_items(client, handle, None, 3, 2)
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
        a_items = await collect_items(client, handle, ["a"], 0, 3)
        assert len(a_items) == 3
        assert a_items[0].offset == 0
        assert a_items[1].offset == 3
        assert a_items[2].offset == 6

        # Subscribe to topic "b" — items are at global offsets 1, 4, 7
        b_items = await collect_items(client, handle, ["b"], 0, 3)
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
            5,
            id=f"pubsub-trunc-error-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Truncate up to offset 3 via update — completion is explicit.
        await handle.execute_update("truncate", 3)

        # Poll from offset 1 (truncated) — should get ApplicationError,
        # NOT crash the workflow task. Catching WorkflowUpdateFailedError is
        # sufficient to prove the handler raised ApplicationError: Temporal's
        # update protocol completes the update with this error only when the
        # handler raises ApplicationError. A bare ValueError (or any other
        # exception) would fail the workflow task instead, causing
        # execute_update to hang — not raise. The follow-up collect_items
        # below proves the workflow task wasn't poisoned.
        with pytest.raises(WorkflowUpdateFailedError) as exc_info:
            await handle.execute_update(
                "__temporal_pubsub_poll",
                PollInput(topics=[], from_offset=1),
                result_type=PollResult,
            )
        cause = exc_info.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "TruncatedOffset"

        # Workflow should still be usable — poll from valid offset 3
        items = await collect_items(client, handle, None, 3, 2)
        assert len(items) == 2
        assert items[0].offset == 3

        await handle.signal("close")


@pytest.mark.asyncio
async def test_truncate_past_end_raises_application_error(client: Client) -> None:
    """truncate() with an offset past the log end raises ApplicationError
    (type=TruncateOutOfRange) — the update surfaces as a clean failure
    without poisoning the workflow task."""
    async with new_worker(
        client,
        TruncateWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateWorkflow.run,
            2,
            id=f"pubsub-trunc-oor-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Only 2 items exist; asking to truncate to offset 5 is out of range.
        with pytest.raises(WorkflowUpdateFailedError) as exc_info:
            await handle.execute_update("truncate", 5)
        cause = exc_info.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "TruncateOutOfRange"

        # Workflow task wasn't poisoned — a valid poll still completes.
        items = await collect_items(client, handle, None, 0, 2)
        assert len(items) == 2

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
            5,
            id=f"pubsub-trunc-recover-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Truncate first 3. The update returns after the handler completes.
        await handle.execute_update("truncate", 3)

        # subscribe from offset 1 (truncated) — should auto-recover
        # and deliver items from base_offset (3)
        pubsub = PubSubClient(handle)
        items: list[PubSubItem] = []
        try:
            async with _async_timeout(5):
                async for item in pubsub.subscribe(
                    from_offset=1, poll_cooldown=0, result_type=bytes
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
        items = await collect_items(client, handle, None, 0, count + 2)
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
        items = await collect_items(client, handle, None, 0, 3, timeout=5.0)
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
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"seed"))]
            ),
        )

        pubsub_client = PubSubClient.create(client, handle.id)
        first_item = asyncio.Event()
        items: list[PubSubItem] = []

        async def subscribe_and_collect() -> None:
            async for item in pubsub_client.subscribe(
                from_offset=0, poll_cooldown=0, result_type=bytes
            ):
                items.append(item)
                first_item.set()

        task = asyncio.create_task(subscribe_and_collect())
        # Bounded wait so a subscribe regression fails fast instead of hanging.
        async with _async_timeout(5):
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
        items = await collect_items(client, handle, None, 0, count, timeout=15.0)
        assert len(items) == count
        for i in range(count):
            assert items[i].data == f"item-{i}".encode()

        await handle.signal(FlushOnExitWorkflow.close)


@pytest.mark.asyncio
async def test_explicit_flush_barrier(client: Client) -> None:
    """``await client.flush()`` is a synchronization point.

    Verifies the documented contract:
      1. Returns immediately when the buffer is empty.
      2. After it returns, items published before the call are durable
         on the workflow side (observable via ``get_offset()``) — even
         when the timer-driven flush would not yet have fired.
      3. Calling it again after a successful flush is a no-op.

    Uses a 60s ``batch_interval`` so a regression where ``flush()``
    silently relies on the background timer surfaces as a hang
    against the test's 5s timeout, not a slow pass.
    """
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-flush-barrier-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        pubsub = PubSubClient.create(client, handle.id, batch_interval=60.0)

        async with _async_timeout(5):
            # 1. Empty-buffer flush is a no-op (must not block).
            assert await pubsub.get_offset() == 0
            await pubsub.flush()
            assert await pubsub.get_offset() == 0

            # 2. Flush makes prior publishes visible without waiting on
            # the 60s batch timer.
            pubsub.publish("events", b"a")
            pubsub.publish("events", b"b")
            pubsub.publish("events", b"c")
            await pubsub.flush()
            assert await pubsub.get_offset() == 3

            # 3. Second flush with no new items is a no-op.
            await pubsub.flush()
            assert await pubsub.get_offset() == 3

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_concurrent_subscribers(client: Client) -> None:
    """Two subscribers on different topics make interleaved progress.

    Publishes A-0, waits for subscriber A to observe it; publishes B-0,
    waits for subscriber B to observe it. At this point both subscribers
    have received exactly one item and are polling for their second,
    so both subscriptions are provably in flight at the same time.
    Then publishes A-1, B-1 the same way. A sequential execution (A drains
    then B starts) cannot satisfy the ordering because B's first item
    isn't published until after A has already received its first.
    """
    async with new_worker(
        client,
        BasicPubSubWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            BasicPubSubWorkflow.run,
            id=f"pubsub-concurrent-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        pubsub = PubSubClient(handle)
        a_items: list[PubSubItem] = []
        b_items: list[PubSubItem] = []
        a_got = [asyncio.Event(), asyncio.Event()]
        b_got = [asyncio.Event(), asyncio.Event()]

        async def collect(
            topic: str,
            collected: list[PubSubItem],
            events: list[asyncio.Event],
        ) -> None:
            async for item in pubsub.subscribe(
                topics=[topic], from_offset=0, poll_cooldown=0, result_type=bytes
            ):
                collected.append(item)
                events[len(collected) - 1].set()
                if len(collected) >= len(events):
                    break

        a_task = asyncio.create_task(collect("a", a_items, a_got))
        b_task = asyncio.create_task(collect("b", b_items, b_got))

        async def publish(topic: str, data: bytes) -> None:
            await handle.signal(
                "__temporal_pubsub_publish",
                PublishInput(items=[PublishEntry(topic=topic, data=_wire_bytes(data))]),
            )

        try:
            async with _async_timeout(10):
                await publish("a", b"a-0")
                await a_got[0].wait()
                await publish("b", b"b-0")
                await b_got[0].wait()
                # Both subscribers are now mid-subscription, each having
                # seen one item and polling for the next.
                await publish("a", b"a-1")
                await a_got[1].wait()
                await publish("b", b"b-1")
                await b_got[1].wait()

            await asyncio.gather(a_task, b_task)
        finally:
            a_task.cancel()
            b_task.cancel()

        assert [i.data for i in a_items] == [b"a-0", b"a-1"]
        assert [i.data for i in b_items] == [b"b-0", b"b-1"]

        await handle.signal(BasicPubSubWorkflow.close)


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
        items = await collect_items(client, handle, None, 0, count + 1, timeout=15.0)
        assert len(items) == count + 1
        for i in range(count):
            assert items[i].data == f"item-{i}".encode()

        # max_batch_size actually engages: at least one flush fires during
        # the publish loop, so 7 items ship as >=2 signals. Without this
        # assertion the test would pass even if max_batch_size were ignored
        # and all 7 items went out in a single exit-time flush (batch_count
        # == 1). Note: max_batch_size is a *trigger* threshold, not a cap —
        # the flusher may take more items from the buffer than max_batch_size
        # if more were added while a prior signal was in flight, so the exact
        # batch count depends on interleaving. Asserting >= 2 is the
        # non-flaky way to verify the mechanism is live.
        seqs = await handle.query(MaxBatchWorkflow.publisher_sequences)
        assert len(seqs) == 1, f"expected one publisher, got {seqs}"
        (batch_count,) = seqs.values()
        assert batch_count >= 2, (
            f"expected >=2 batches with max_batch_size=3 and 7 items, got "
            f"{batch_count} — max_batch_size did not trigger a mid-loop flush"
        )

        await handle.signal(MaxBatchWorkflow.close)


@pytest.mark.asyncio
async def test_replay_safety(client: Client) -> None:
    """Pub/sub broker survives workflow replay (max_cached_workflows=0)."""
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
        items = await collect_items(client, handle, None, 0, 7)
        # Full ordered sequence — endpoint-only checks would miss mid-stream
        # replay corruption (reordering, duplication, dropped items).
        assert [i.data for i in items] == [
            b"started",
            b"item-0",
            b"item-1",
            b"item-2",
            b"item-3",
            b"item-4",
            b"done",
        ]
        assert [i.offset for i in items] == list(range(7))
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

        items = await collect_items(client, handle, None, 0, 3)
        assert [i.data for i in items] == [b"item-0", b"item-1", b"item-2"]

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_flush_raises_after_max_retry_duration(client: Client) -> None:
    """When max_retry_duration is exceeded, flush raises TimeoutError and the
    client can resume publishing without losing subsequent items."""
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
        with (
            patch(
                "temporalio.contrib.pubsub._client.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch.object(handle, "signal", side_effect=maybe_failing_signal),
        ):
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

        items = await collect_items(client, handle, None, 0, 1)
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
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"item-0"))],
                publisher_id="test-pub",
                sequence=1,
            ),
        )

        # Send the same sequence again — should be deduped
        await handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"duplicate"))],
                publisher_id="test-pub",
                sequence=1,
            ),
        )

        # Send a new sequence — should go through
        await handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"item-1"))],
                publisher_id="test-pub",
                sequence=2,
            ),
        )

        # Should have 2 items, not 3 (collect_items' update call acts as barrier)
        items = await collect_items(client, handle, None, 0, 2)
        assert len(items) == 2
        assert items[0].data == b"item-0"
        assert items[1].data == b"item-1"

        # Verify offset is 2 (not 3)
        pubsub_client = PubSubClient(handle)
        offset = await pubsub_client.get_offset()
        assert offset == 2

        await handle.signal(BasicPubSubWorkflow.close)


@pytest.mark.asyncio
async def test_double_init_raises(client: Client) -> None:
    """Instantiating PubSub twice from @workflow.init raises RuntimeError.

    The first PubSub() registers the __temporal_pubsub_publish signal handler; the
    second call detects the existing handler and raises rather than
    silently overwriting it.
    """
    async with new_worker(client, DoubleInitWorkflow) as worker:
        handle = await client.start_workflow(
            DoubleInitWorkflow.run,
            id=f"pubsub-double-init-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        err = await handle.query(DoubleInitWorkflow.get_double_init_error)
        assert err is not None
        assert "already registered" in err
        await handle.signal(DoubleInitWorkflow.close)


@pytest.mark.asyncio
async def test_pubsub_outside_init_raises(client: Client) -> None:
    """Constructing PubSub outside @workflow.init raises RuntimeError.

    The workflow calls PubSub() from @workflow.run; the caller-frame
    guard must reject the call because the caller's function name is
    ``run``, not ``__init__``.
    """
    async with new_worker(client, LatePubSubWorkflow) as worker:
        result = await client.execute_workflow(
            LatePubSubWorkflow.run,
            id=f"pubsub-late-init-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert "must be constructed directly from the workflow's" in result
        assert "'run'" in result


@pytest.mark.asyncio
async def test_truncate_pubsub(client: Client) -> None:
    """PubSub.truncate discards prefix and adjusts base_offset."""
    async with new_worker(
        client,
        TruncateWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TruncateWorkflow.run,
            5,
            id=f"pubsub-truncate-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Verify all 5 items
        items = await collect_items(client, handle, None, 0, 5)
        assert len(items) == 5

        # Truncate up to offset 3 (discard items 0, 1, 2). The update
        # returns after the handler completes.
        await handle.execute_update("truncate", 3)

        # Offset should still be 5 (truncation moves base_offset, not tail)
        pubsub_client = PubSubClient(handle)
        offset = await pubsub_client.get_offset()
        assert offset == 5

        # Reading from offset 3 should work (items 3, 4)
        items_after = await collect_items(client, handle, None, 3, 2)
        assert len(items_after) == 2
        assert items_after[0].data == b"item-3"
        assert items_after[1].data == b"item-4"

        await handle.signal("close")


@pytest.mark.asyncio
async def test_ttl_pruning_in_get_pubsub_state(client: Client) -> None:
    """PubSub.get_state prunes publishers whose last-seen time exceeds the
    TTL while retaining newer publishers. The log itself is unaffected.

    Uses a wall-clock gap between publishes so that workflow.time()
    advances between the two publishers' tasks. workflow.time() can't be
    cleanly injected from outside, so a short real sleep is the mechanism.
    """
    async with new_worker(
        client,
        TTLTestWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            TTLTestWorkflow.run,
            id=f"pubsub-ttl-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # pub-old arrives first.
        await handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"old"))],
                publisher_id="pub-old",
                sequence=1,
            ),
        )

        # Sanity: pub-old is recorded (generous TTL retains it).
        state_before = await handle.query(TTLTestWorkflow.get_state_with_ttl, 9999.0)
        assert "pub-old" in state_before.publisher_sequences

        # Let workflow.time() advance by real wall-clock time. Use a
        # generous gap (1.0s) relative to the TTL (0.5s) so the test
        # tolerates CI scheduling delays — pub-old must be >=0.5s past,
        # pub-new must be <0.5s past, at the moment of the query.
        await asyncio.sleep(1.0)

        # pub-new arrives after the gap.
        await handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[PublishEntry(topic="events", data=_wire_bytes(b"new"))],
                publisher_id="pub-new",
                sequence=1,
            ),
        )

        # TTL=0.5s prunes pub-old (~1.0s old) but keeps pub-new (~0s).
        state = await handle.query(TTLTestWorkflow.get_state_with_ttl, 0.5)
        assert "pub-old" not in state.publisher_sequences
        assert "pub-new" in state.publisher_sequences
        # Log contents are not touched by publisher pruning.
        assert len(state.log) == 2

        await handle.signal("close")


# ---------------------------------------------------------------------------
# Truncate and TTL test workflows
# ---------------------------------------------------------------------------


@workflow.defn
class TruncateWorkflow:
    """Test scaffolding that exposes PubSub.truncate via a user-authored
    update.

    The contrib module does not define a built-in external truncate API —
    truncation is a workflow-internal decision (typically driven by
    consumer progress or a retention policy). Workflows that want external
    control wire up their own signal or update. We use an update here so
    callers get explicit completion (signals are fire-and-forget).

    The ``truncate`` update is ``async`` and opens with
    ``await asyncio.sleep(0)`` — the documented recipe from the
    contrib/pubsub README for sync-shaped handlers that read ``PubSub``
    state. The yield lets any buffered ``__temporal_pubsub_publish`` signal in
    the same activation apply before the handler inspects ``self._log``.
    This keeps the test workflow aligned with the pattern users are
    directed to follow.

    ``prepub_count`` seeds the log with N byte-payload items during
    ``@workflow.init`` as test convenience, so the error-path tests
    have deterministic log content without an extra round trip to
    publish from the client.
    """

    @workflow.init
    def __init__(self, prepub_count: int = 0) -> None:
        self.pubsub = PubSub()
        self._closed = False
        for i in range(prepub_count):
            self.pubsub.publish("events", f"item-{i}".encode())

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.update
    async def truncate(self, up_to_offset: int) -> None:
        # Recipe from README.md "Gotcha" section: yield once so any
        # buffered __temporal_pubsub_publish in the same activation applies
        # before we read self._log. asyncio.sleep(0) is a pure asyncio
        # yield — no Temporal timer, no history event.
        await asyncio.sleep(0)
        self.pubsub.truncate(up_to_offset)

    @workflow.run
    async def run(self, _prepub_count: int = 0) -> None:
        # _prepub_count is consumed in @workflow.init above. @workflow.run
        # must accept the same positional args, but the names are free
        # to differ.
        del _prepub_count
        await workflow.wait_condition(lambda: self._closed)


@workflow.defn
class TTLTestWorkflow:
    """Workflow that exposes PubSub.get_state via query for TTL testing."""

    @workflow.init
    def __init__(self) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def get_state_with_ttl(self, ttl: float) -> PubSubState:
        return self.pubsub.get_state(publisher_ttl=ttl)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._closed)


# ---------------------------------------------------------------------------
# Continue-as-new workflow and test
# ---------------------------------------------------------------------------


@dataclass
class CANWorkflowInputTyped:
    """Uses proper typing."""

    pubsub_state: PubSubState | None = None


@workflow.defn
class ContinueAsNewTypedWorkflow:
    """CAN workflow using properly-typed pubsub_state."""

    @workflow.init
    def __init__(self, input: CANWorkflowInputTyped) -> None:
        self.pubsub = PubSub(prior_state=input.pubsub_state)
        self._should_continue = False
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.signal
    def trigger_continue(self) -> None:
        self._should_continue = True

    @workflow.query
    def publisher_sequences(self) -> dict[str, int]:
        return dict(self.pubsub._publisher_sequences)

    @workflow.run
    async def run(
        self,
        input: CANWorkflowInputTyped,  # type:ignore[reportUnusedParameter]
    ) -> None:
        while True:
            await workflow.wait_condition(lambda: self._should_continue or self._closed)
            if self._closed:
                return
            if self._should_continue:
                self._should_continue = False
                self.pubsub.drain()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(
                    args=[
                        CANWorkflowInputTyped(
                            pubsub_state=self.pubsub.get_state(),
                        )
                    ]
                )


@pytest.mark.asyncio
async def test_continue_as_new_properly_typed(client: Client) -> None:
    """CAN preserves the log, global offsets, AND publisher dedup state
    when pubsub_state is properly typed as ``PubSubState | None``."""
    async with new_worker(
        client,
        ContinueAsNewTypedWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            ContinueAsNewTypedWorkflow.run,
            CANWorkflowInputTyped(),
            id=f"pubsub-can-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Publish 3 items with an explicit publisher_id/sequence so dedup
        # state is seeded and we can verify it survives CAN.
        await handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[
                    PublishEntry(topic="events", data=_wire_bytes(b"item-0")),
                    PublishEntry(topic="events", data=_wire_bytes(b"item-1")),
                    PublishEntry(topic="events", data=_wire_bytes(b"item-2")),
                ],
                publisher_id="pub",
                sequence=1,
            ),
        )

        items_before = await collect_items(client, handle, None, 0, 3)
        assert len(items_before) == 3

        await handle.signal(ContinueAsNewTypedWorkflow.trigger_continue)

        new_handle = client.get_workflow_handle(handle.id)
        await assert_eq_eventually(
            True,
            lambda: _is_different_run(handle, new_handle),
        )

        # Log contents and offsets preserved across CAN.
        items_after = await collect_items(client, new_handle, None, 0, 3)
        assert [i.data for i in items_after] == [b"item-0", b"item-1", b"item-2"]
        assert [i.offset for i in items_after] == [0, 1, 2]

        # Dedup state preserved: the carried publisher_sequences dict has
        # pub -> 1 after CAN.
        seqs_after_can = await new_handle.query(
            ContinueAsNewTypedWorkflow.publisher_sequences
        )
        assert seqs_after_can == {"pub": 1}

        # Re-sending publisher_id="pub", sequence=1 must be rejected by
        # dedup — both the log and the publisher_sequences entry stay put.
        await new_handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[
                    PublishEntry(topic="events", data=_wire_bytes(b"dup")),
                ],
                publisher_id="pub",
                sequence=1,
            ),
        )
        seqs_after_dup = await new_handle.query(
            ContinueAsNewTypedWorkflow.publisher_sequences
        )
        assert seqs_after_dup == {"pub": 1}

        # A fresh sequence from the same publisher is accepted, advances
        # publisher_sequences to 2, and the new item gets offset 3.
        await new_handle.signal(
            "__temporal_pubsub_publish",
            PublishInput(
                items=[
                    PublishEntry(topic="events", data=_wire_bytes(b"item-3")),
                ],
                publisher_id="pub",
                sequence=2,
            ),
        )
        seqs_after_accept = await new_handle.query(
            ContinueAsNewTypedWorkflow.publisher_sequences
        )
        assert seqs_after_accept == {"pub": 2}
        items_all = await collect_items(client, new_handle, None, 0, 4)
        assert [i.data for i in items_all] == [
            b"item-0",
            b"item-1",
            b"item-2",
            b"item-3",
        ]
        assert items_all[3].offset == 3

        await new_handle.signal(ContinueAsNewTypedWorkflow.close)


# ---------------------------------------------------------------------------
# Cross-workflow pub/sub (Scenario 1)
# ---------------------------------------------------------------------------


@dataclass
class CrossWorkflowInput:
    broker_workflow_id: str
    expected_count: int


@workflow.defn
class BrokerWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> None:
        for i in range(count):
            self.pubsub.publish("events", f"broker-{i}".encode())
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
    async with _async_timeout(15.0):
        async for item in client.subscribe(
            topics=["events"], from_offset=0, poll_cooldown=0, result_type=bytes
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
        external_items = await collect_items(
            client, broker_handle, ["events"], 0, count
        )
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
class NexusBrokerWorkflow:
    @workflow.init
    def __init__(self, count: int) -> None:
        self.pubsub = PubSub()
        self._closed = False

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.run
    async def run(self, count: int) -> str:
        for i in range(count):
            self.pubsub.publish("events", f"nexus-{i}".encode())
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
                "__temporal_pubsub_publish",
                PublishInput(
                    items=[PublishEntry(topic="big", data=_wire_bytes(chunk))]
                ),
            )

        # First poll from offset 0 — should get some items but not all.
        # (The update acts as a barrier for all prior publish signals.)
        result1: PollResult = await handle.execute_update(
            "__temporal_pubsub_poll",
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
                "__temporal_pubsub_poll",
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
                "__temporal_pubsub_publish",
                PublishInput(
                    items=[PublishEntry(topic="big", data=_wire_bytes(chunk))]
                ),
            )

        # subscribe() should seamlessly iterate through all 8 items
        items = await collect_items(client, handle, None, 0, 8, timeout=10.0)
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
            items = await collect_items(
                handler_client, broker_handle, ["events"], 0, count
            )
            assert len(items) == count
            for i in range(count):
                assert items[i].topic == "events"
                assert items[i].data == f"nexus-{i}".encode()

            # Clean up — signal broker to close so caller can complete
            await broker_handle.signal("close")
            result = await caller_handle.result()
            assert result == "done"
