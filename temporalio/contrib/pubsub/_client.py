"""External-side pub/sub client.

Used by activities, starters, and any code with a workflow handle to publish
messages and subscribe to topics on a pub/sub workflow.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Self

import logging

from temporalio import activity
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)

from ._types import PollInput, PollResult, PubSubItem, PublishEntry, PublishInput

logger = logging.getLogger(__name__)


class PubSubClient:
    """Client for publishing to and subscribing from a pub/sub workflow.

    For publishing, use as an async context manager to get automatic batching
    with a background flush timer::

        async with PubSubClient(handle, batch_interval=2.0) as client:
            client.publish("events", b"hello")
            client.publish("events", b"world", priority=True)  # flushes immediately

    For subscribing::

        client = PubSubClient(handle)
        async for item in client.subscribe(["events"], from_offset=0):
            process(item)
    """

    def __init__(
        self,
        handle: WorkflowHandle,
        batch_interval: float = 2.0,
        max_batch_size: int | None = None,
    ) -> None:
        self._handle = handle
        self._batch_interval = batch_interval
        self._max_batch_size = max_batch_size
        self._buffer: list[PublishEntry] = []
        self._flush_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None

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
        """Send all buffered messages to the workflow via signal."""
        if self._buffer:
            batch = self._buffer.copy()
            self._buffer.clear()
            await self._handle.signal(
                "__pubsub_publish", PublishInput(items=batch)
            )

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
        """Async iterator that polls for new items.

        Args:
            topics: Topic filter. None or empty list means all topics.
            from_offset: Global offset to start reading from.
            follow_continues: If True, automatically follow continue-as-new
                chains. The subscriber re-targets the new run and retries
                from the same offset.

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
            except WorkflowUpdateRPCTimeoutOrCancelledError:
                if follow_continues and await self._follow_continue_as_new():
                    continue  # retry poll against new run
                return
            except Exception:
                if follow_continues and await self._follow_continue_as_new():
                    continue  # retry poll against new run
                raise
            for item in result.items:
                yield item
            offset = result.next_offset

    async def _follow_continue_as_new(self) -> bool:
        """Check if the workflow continued-as-new and update the handle.

        Returns True if the handle was updated (caller should retry).
        """
        try:
            desc = await self._handle.describe()
        except Exception:
            return False
        if desc.status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
            self._handle = self._handle._client.get_workflow_handle(
                self._handle.id
            )
            return True
        return False

    async def get_offset(self) -> int:
        """Query the current log offset (length)."""
        return await self._handle.query("__pubsub_offset", result_type=int)


def activity_pubsub_client(**kwargs: object) -> PubSubClient:
    """Create a PubSubClient for the current activity's parent workflow.

    Must be called from within an activity. Passes all kwargs to PubSubClient.
    """
    info = activity.info()
    workflow_id = info.workflow_id
    assert workflow_id is not None, "activity must be called from within a workflow"
    handle = activity.client().get_workflow_handle(workflow_id)
    return PubSubClient(handle, **kwargs)  # type: ignore[arg-type]
