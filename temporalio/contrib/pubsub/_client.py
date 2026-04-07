"""External-side pub/sub client.

Used by activities, starters, and any code with a workflow handle to publish
messages and subscribe to topics on a pub/sub workflow.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Self

from temporalio import activity
from temporalio.client import (
    Client,
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

        Prefer :py:meth:`for_workflow` when you need ``follow_continues``
        in ``subscribe()``.

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

    @classmethod
    def for_workflow(
        cls,
        client: Client,
        workflow_id: str,
        *,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
    ) -> PubSubClient:
        """Create a pub/sub client from a Temporal client and workflow ID.

        This is the preferred constructor. It enables ``follow_continues``
        in ``subscribe()`` because it can construct fresh handles after
        continue-as-new.

        Args:
            client: Temporal client.
            workflow_id: ID of the pub/sub workflow.
            batch_interval: Seconds between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
        """
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

        Items are removed from the buffer only after the signal succeeds.
        If the signal fails, the items remain buffered for retry.
        """
        #@AGENT: is it possible to have a second invocation of flush while the first is running?
        if self._buffer:
            batch = list(self._buffer)
            await self._handle.signal(
                "__pubsub_publish", PublishInput(items=batch)
            )
            del self._buffer[: len(batch)]

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
        follow_continues: bool = True,
    ) -> AsyncIterator[PubSubItem]:
        #@AGENT: why would we not always follow CAN chains? How is the client supposed to know whether the workflow does CAN?
        """Async iterator that polls for new items.

        Args:
            topics: Topic filter. None or empty list means all topics.
            from_offset: Global offset to start reading from.
            follow_continues: If True and the client was created via
                :py:meth:`for_workflow`, automatically follow
                continue-as-new chains.

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
                #@AGENT: help me understand what this means / how we respond
                return
            except WorkflowUpdateRPCTimeoutOrCancelledError:
                #@AGENT: is this code path tested?
                if follow_continues and self._follow_continue_as_new():
                    continue
                return
            for item in result.items:
                yield item
            offset = result.next_offset
            #@AGENT: do we want to create a provision for putting a little bit of sleep in here to rate limit the polls when we have a workflow publisher (no batching). note that the alternative is to put a timer in the workflow (costing another activity)

    def _follow_continue_as_new(self) -> bool:
        """Re-target the handle to the latest run if client is available."""
        if self._client is None:
            return False
        #@AGENT: put a description of what is going on here and why
        self._handle = self._client.get_workflow_handle(self._workflow_id)
        return True

    #@AGENT: should this be part of the interface?
    async def get_offset(self) -> int:
        """Query the current log offset (length)."""
        return await self._handle.query("__pubsub_offset", result_type=int)


#@AGENT: can we detect the activity context automatically and move this functionality into for_workflow?, e.g., just make the client optional if you are running in an activity
def activity_pubsub_client(
    batch_interval: float = 2.0,
    max_batch_size: int | None = None,
) -> PubSubClient:
    """Create a PubSubClient for the current activity's parent workflow.

    Must be called from within an activity. Uses :py:meth:`PubSubClient.for_workflow`
    so ``follow_continues`` works in ``subscribe()``.

    Args:
        batch_interval: Seconds between automatic flushes.
        max_batch_size: Auto-flush when buffer reaches this size.
    """
    info = activity.info()
    workflow_id = info.workflow_id
    assert workflow_id is not None, "activity must be called from within a workflow"
    return PubSubClient.for_workflow(
        activity.client(),
        workflow_id,
        batch_interval=batch_interval,
        max_batch_size=max_batch_size,
    )
