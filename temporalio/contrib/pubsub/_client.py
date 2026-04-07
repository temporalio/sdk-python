"""External-side pub/sub client.

Used by activities, starters, and any code with a workflow handle to publish
messages and subscribe to topics on a pub/sub workflow.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Self

from temporalio import activity
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)

from ._types import PollInput, PollResult, PubSubItem, PublishEntry, PublishInput


class PubSubClient:
    """Client for publishing to and subscribing from a pub/sub workflow.

    Create via :py:meth:`for_workflow` (preferred) or by passing a handle
    directly to the constructor.

    For publishing, use as an async context manager to get automatic batching::

        client = PubSubClient.for_workflow(temporal_client, workflow_id)
        async with client:
            client.publish("events", b"hello")
            client.publish("events", b"world", priority=True)

    For subscribing::

        client = PubSubClient.for_workflow(temporal_client, workflow_id)
        async for item in client.subscribe(["events"], from_offset=0):
            process(item)
    """

    def __init__(
        self,
        handle: WorkflowHandle,
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
    ) -> None:
        """Create a pub/sub client from a workflow handle.

        Prefer :py:meth:`for_workflow` when you need continue-as-new
        following in ``subscribe()``.

        Args:
            handle: Workflow handle to the pub/sub workflow.
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
        """
        self._handle = handle
        self._client: Client | None = None
        self._workflow_id = handle.id
        self._batch_interval = batch_interval
        self._max_batch_size = max_batch_size
        self._buffer: list[PublishEntry] = []
        self._flush_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()
        self._publisher_id: str = uuid.uuid4().hex
        self._sequence: int = 0

    @classmethod
    def for_workflow(
        cls,
        client: Client | None = None,
        workflow_id: str | None = None,
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
    ) -> PubSubClient:
        """Create a pub/sub client from a Temporal client and workflow ID.

        This is the preferred constructor. It enables continue-as-new
        following in ``subscribe()``.

        If called from within an activity, ``client`` and ``workflow_id``
        can be omitted — they are inferred from the activity context.

        Args:
            client: Temporal client. If None and in an activity, uses
                ``activity.client()``.
            workflow_id: ID of the pub/sub workflow. If None and in an
                activity, uses the activity's parent workflow ID.
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
        """
        if client is None or workflow_id is None:
            info = activity.info()
            if client is None:
                client = activity.client()
            if workflow_id is None:
                wf_id = info.workflow_id
                assert wf_id is not None, (
                    "activity must be called from within a workflow"
                )
                workflow_id = wf_id
        handle = client.get_workflow_handle(workflow_id)
        instance = cls(
            handle, batch_interval=batch_interval, max_batch_size=max_batch_size
        )
        instance._client = client
        return instance

    async def __aenter__(self) -> Self:
        self._flush_task = asyncio.create_task(self._run_flusher())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()

    def publish(self, topic: str, data: bytes, priority: bool = False) -> None:
        """Buffer a message for publishing.

        Args:
            topic: Topic string.
            data: Opaque byte payload.
            priority: If True, wake the flusher to send immediately.
        """
        self._buffer.append(PublishEntry(topic=topic, data=data))
        if priority or (
            self._max_batch_size is not None
            and len(self._buffer) >= self._max_batch_size
        ):
            self._flush_event.set()

    async def flush(self) -> None:
        """Send all buffered messages to the workflow via signal.

        Uses a lock to serialize concurrent flushes. If a flush is already
        in progress, callers wait on the lock — by the time they enter,
        their items (plus any others added meanwhile) are in the buffer
        and get sent in one signal. This naturally coalesces N concurrent
        flush calls into fewer signals.

        Uses buffer swap for exactly-once delivery. On failure, items are
        restored to the buffer but the sequence is NOT decremented — the
        next flush gets a new sequence number. This prevents data loss
        when the signal was delivered but the client saw an error: newly
        buffered items that arrived during the failed await must not be
        sent under the old (already-delivered) sequence, or the workflow
        would deduplicate them away.
        """
        async with self._flush_lock:
            if not self._buffer:
                return
            self._sequence += 1
            batch = self._buffer
            self._buffer = []
            try:
                await self._handle.signal(
                    "__pubsub_publish",
                    PublishInput(
                        items=batch,
                        publisher_id=self._publisher_id,
                        sequence=self._sequence,
                    ),
                )
            except Exception:
                self._buffer = batch + self._buffer
                raise

    async def _run_flusher(self) -> None:
        """Background task: wait for timer OR priority wakeup, then flush."""
        while True:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._batch_interval
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            await self.flush()

    async def subscribe(
        self,
        topics: list[str] | None = None,
        from_offset: int = 0,
        *,
        poll_interval: float = 0.1,
    ) -> AsyncIterator[PubSubItem]:
        """Async iterator that polls for new items.

        Automatically follows continue-as-new chains when the client
        was created via :py:meth:`for_workflow`.

        Args:
            topics: Topic filter. None or empty list means all topics.
            from_offset: Global offset to start reading from.
            poll_interval: Seconds to sleep between polls to avoid
                overwhelming the workflow when items arrive faster than
                the poll round-trip. Defaults to 0.1.

        Yields:
            PubSubItem for each matching item.
        """
        offset = from_offset
        while True:
            try:
                result: PollResult = await self._handle.execute_update(
                    "__pubsub_poll",
                    PollInput(topics=topics or [], from_offset=offset),
                    result_type=PollResult,
                )
            except asyncio.CancelledError:
                # The caller's task was cancelled (e.g., activity shutdown
                # or subscriber cleanup). Stop iteration gracefully.
                return
            except WorkflowUpdateRPCTimeoutOrCancelledError:
                # The update was cancelled server-side — possibly due to
                # continue-as-new (the drain validator rejected the poll).
                # Check if the workflow CAN'd and follow the chain.
                if await self._follow_continue_as_new():
                    continue
                return
            for item in result.items:
                yield item
            offset = result.next_offset
            if poll_interval > 0:
                await asyncio.sleep(poll_interval)

    async def _follow_continue_as_new(self) -> bool:
        """Check if the workflow continued-as-new and re-target the handle.

        When a poll fails, this method checks the workflow's execution
        status. If it's CONTINUED_AS_NEW, we get a fresh handle for the
        same workflow ID (no pinned run_id), which targets the latest run.
        The subscriber can then retry the poll from the same offset — the
        new run's log contains all items from the previous run.

        Returns True if the handle was updated (caller should retry).
        """
        if self._client is None:
            return False
        try:
            desc = await self._handle.describe()
        except Exception:
            return False
        if desc.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
            self._handle = self._client.get_workflow_handle(self._workflow_id)
            return True
        return False

    async def get_offset(self) -> int:
        """Query the current global offset (base_offset + log length)."""
        return await self._handle.query("__pubsub_offset", result_type=int)
