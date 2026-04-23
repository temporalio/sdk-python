"""External-side pub/sub client.

Used by activities, starters, and any code with a workflow handle to publish
messages and subscribe to topics on a pub/sub workflow.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Self

from temporalio import activity
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)

from ._types import (
    PollInput,
    PollResult,
    PublishEntry,
    PublishInput,
    PubSubItem,
    decode_data,
    encode_data,
)


class PubSubClient:
    """Client for publishing to and subscribing from a pub/sub workflow.

    Create via :py:meth:`create` (explicit client + workflow id),
    :py:meth:`from_activity` (infer both from the current activity
    context), or by passing a handle directly to the constructor.

    For publishing, use as an async context manager to get automatic batching::

        client = PubSubClient.create(temporal_client, workflow_id)
        async with client:
            client.publish("events", b"hello")
            client.publish("events", b"world", force_flush=True)

    For subscribing::

        client = PubSubClient.create(temporal_client, workflow_id)
        async for item in client.subscribe(["events"], from_offset=0):
            process(item)
    """

    def __init__(
        self,
        handle: WorkflowHandle[Any, Any],
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
        max_retry_duration: float = 600.0,
    ) -> None:
        """Create a pub/sub client from a workflow handle.

        Prefer :py:meth:`create` — it enables continue-as-new following in
        ``subscribe()``. The direct-handle form used here does not follow
        CAN and will stop yielding once the original run ends.

        Args:
            handle: Workflow handle to the pub/sub workflow.
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum seconds to retry a failed flush
                before raising TimeoutError. Must be less than the
                workflow's ``publisher_ttl`` (default 900s) to preserve
                exactly-once delivery. Default: 600s.
        """
        self._handle: WorkflowHandle[Any, Any] = handle
        self._client: Client | None = None
        self._workflow_id = handle.id
        self._batch_interval = batch_interval
        self._max_batch_size = max_batch_size
        self._max_retry_duration = max_retry_duration
        self._buffer: list[PublishEntry] = []
        self._flush_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()
        self._publisher_id: str = uuid.uuid4().hex[:16]
        self._sequence: int = 0
        self._pending: list[PublishEntry] | None = None
        self._pending_seq: int = 0
        self._pending_since: float | None = None

    @classmethod
    def create(
        cls,
        client: Client,
        workflow_id: str,
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
        max_retry_duration: float = 600.0,
    ) -> PubSubClient:
        """Create a pub/sub client from a Temporal client and workflow ID.

        Use this when the caller has an explicit ``Client`` and
        ``workflow_id`` in hand (starters, BFFs, other workflows'
        activities). For code running inside an activity that targets its
        own parent workflow, see :py:meth:`from_activity`.

        A client created through this method follows continue-as-new
        chains in ``subscribe()``.

        Args:
            client: Temporal client.
            workflow_id: ID of the pub/sub workflow.
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum seconds to retry a failed flush
                before raising TimeoutError. Default: 600s.
        """
        handle = client.get_workflow_handle(workflow_id)
        instance = cls(
            handle,
            batch_interval=batch_interval,
            max_batch_size=max_batch_size,
            max_retry_duration=max_retry_duration,
        )
        instance._client = client
        return instance

    @classmethod
    def from_activity(
        cls,
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
        max_retry_duration: float = 600.0,
    ) -> PubSubClient:
        """Create a pub/sub client targeting the current activity's parent workflow.

        Must be called from within an activity. The Temporal client and
        parent workflow id are taken from the activity context.

        Args:
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum seconds to retry a failed flush
                before raising TimeoutError. Default: 600s.
        """
        info = activity.info()
        workflow_id = info.workflow_id
        assert (
            workflow_id is not None
        ), "from_activity requires an activity with a parent workflow"
        return cls.create(
            activity.client(),
            workflow_id,
            batch_interval=batch_interval,
            max_batch_size=max_batch_size,
            max_retry_duration=max_retry_duration,
        )

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
        # Drain both pending and buffer. A single _flush() processes either
        # pending OR buffer, not both — so if the flusher was cancelled
        # mid-signal (pending set) while the producer added more items
        # (buffer non-empty), a single final flush would orphan the buffer.
        while self._pending is not None or self._buffer:
            await self._flush()

    def publish(self, topic: str, data: bytes, force_flush: bool = False) -> None:
        """Buffer a message for publishing.

        Args:
            topic: Topic string.
            data: Opaque byte payload.
            force_flush: If True, wake the flusher to send immediately
                (fire-and-forget — does not block the caller).
        """
        self._buffer.append(PublishEntry(topic=topic, data=encode_data(data)))
        if force_flush or (
            self._max_batch_size is not None
            and len(self._buffer) >= self._max_batch_size
        ):
            self._flush_event.set()

    async def _flush(self) -> None:
        """Send buffered or pending messages to the workflow via signal.

        On failure, the pending batch and sequence are kept for retry.
        Only advances the confirmed sequence on success.
        """
        async with self._flush_lock:
            if self._pending is not None:
                # Retry path: check max_retry_duration
                if (
                    self._pending_since is not None
                    and time.monotonic() - self._pending_since
                    > self._max_retry_duration
                ):
                    # Advance confirmed sequence so the next batch gets
                    # a fresh sequence number. Without this, the next batch
                    # reuses pending_seq, which the workflow may have already
                    # accepted — causing silent dedup (data loss).
                    # See DropPendingFixed / SequenceFreshness in the design doc.
                    self._sequence = self._pending_seq
                    self._pending = None
                    self._pending_seq = 0
                    self._pending_since = None
                    raise TimeoutError(
                        f"Flush retry exceeded max_retry_duration "
                        f"({self._max_retry_duration}s). Pending batch dropped. "
                        f"If the signal was delivered, items are in the log. "
                        f"If not, they are lost."
                    )
                batch = self._pending
                seq = self._pending_seq
            elif self._buffer:
                # New batch path
                seq = self._sequence + 1
                batch = self._buffer
                self._buffer = []
                self._pending = batch
                self._pending_seq = seq
                self._pending_since = time.monotonic()
            else:
                return

            try:
                await self._handle.signal(
                    "__pubsub_publish",
                    PublishInput(
                        items=batch,
                        publisher_id=self._publisher_id,
                        sequence=seq,
                    ),
                )
                # Success: advance confirmed sequence, clear pending
                self._sequence = seq
                self._pending = None
                self._pending_seq = 0
                self._pending_since = None
            except Exception:
                # Pending stays set for retry on the next _flush() call
                raise

    async def _run_flusher(self) -> None:
        """Background task: wait for timer OR force_flush wakeup, then flush."""
        while True:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(), timeout=self._batch_interval
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            await self._flush()

    async def subscribe(
        self,
        topics: list[str] | None = None,
        from_offset: int = 0,
        *,
        poll_cooldown: float = 0.1,
    ) -> AsyncIterator[PubSubItem]:
        """Async iterator that polls for new items.

        Automatically follows continue-as-new chains when the client
        was created via :py:meth:`create`.

        Args:
            topics: Topic filter. None or empty list means all topics.
            from_offset: Global offset to start reading from.
            poll_cooldown: Minimum seconds between polls to avoid
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
                return
            except WorkflowUpdateFailedError as e:
                if e.cause and getattr(e.cause, "type", None) == "TruncatedOffset":
                    # Subscriber fell behind truncation. Retry from offset 0
                    # which the mixin treats as "from the beginning of
                    # whatever exists" (i.e., from base_offset).
                    offset = 0
                    continue
                raise
            except WorkflowUpdateRPCTimeoutOrCancelledError:
                if await self._follow_continue_as_new():
                    continue
                return
            for wire_item in result.items:
                yield PubSubItem(
                    topic=wire_item.topic,
                    data=decode_data(wire_item.data),
                    offset=wire_item.offset,
                )
            offset = result.next_offset
            if not result.more_ready and poll_cooldown > 0:
                await asyncio.sleep(poll_cooldown)

    async def _follow_continue_as_new(self) -> bool:
        """Check if the workflow continued-as-new and re-target the handle.

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
