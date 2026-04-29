"""External-side client for Workflow Streams.

Used by activities, starters, and any code with a workflow handle to
publish messages and subscribe to topics on a workflow that hosts a
:class:`WorkflowStream`.

Each published value is turned into a :class:`Payload` via the client's
sync payload converter. The **codec chain** (e.g. encryption, compression)
is **not** run per item — it runs once at the envelope
level when Temporal's SDK encodes the ``__temporal_workflow_stream_publish``
signal args and the ``__temporal_workflow_stream_poll`` update result.
Running the codec per item as well would double-encrypt / double-compress,
because the envelope path covers the items again. The per-item
``Payload`` still carries the encoding metadata (``encoding: json/plain``,
``messageType``, etc.) required by ``subscribe(result_type=T)`` on the
consumer side.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from typing_extensions import Self

from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.converter import DataConverter, PayloadConverter

from ._types import (
    PollInput,
    PollResult,
    PublishEntry,
    PublishInput,
    WorkflowStreamItem,
    _decode_payload,
    _encode_payload,
)


class WorkflowStreamClient:
    """Client for publishing to and subscribing from a workflow stream.

    .. warning::
        This class is experimental and may change in future versions.

    Create via :py:meth:`create` (explicit client + workflow id),
    :py:meth:`from_activity` (infer both from the current activity
    context), or by passing a handle directly to the constructor.

    For publishing, use as an async context manager to get automatic
    batching::

        client = WorkflowStreamClient.create(temporal_client, workflow_id)
        async with client:
            client.publish("events", my_event)
            client.publish("events", another_event, force_flush=True)
            ...  # more publishing
        # Buffer is flushed automatically on context manager exit.

    For subscribing::

        client = WorkflowStreamClient.create(temporal_client, workflow_id)
        async for item in client.subscribe(["events"], result_type=MyEvent):
            process(item.data)
    """

    def __init__(
        self,
        handle: WorkflowHandle[Any, Any],
        *,
        client: Client | None = None,
        batch_interval: timedelta = timedelta(seconds=2),
        max_batch_size: int | None = None,
        max_retry_duration: timedelta = timedelta(seconds=600),
    ) -> None:
        """Create a stream client from a workflow handle.

        Prefer :py:meth:`create` — it enables continue-as-new following
        in ``subscribe()`` and supplies the :class:`Client` needed to
        reach the data converter chain.

        Args:
            handle: Workflow handle to the workflow hosting the stream.
            client: Temporal client whose payload converter will be used
                to turn published values into ``Payload`` objects and to
                decode subscriptions when ``result_type`` is set. The
                codec chain is **not** applied per item (doing so would
                double-encrypt — see module docstring). If ``None``, the
                default payload converter is used.
            batch_interval: Interval between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum time to retry a failed flush
                before raising TimeoutError. Must be less than the
                workflow's ``publisher_ttl`` (default 15 minutes) to
                preserve exactly-once delivery. Default: 10 minutes.
        """
        self._handle: WorkflowHandle[Any, Any] = handle
        self._client: Client | None = client
        self._workflow_id = handle.id
        self._batch_interval = batch_interval
        self._max_batch_size = max_batch_size
        self._max_retry_duration = max_retry_duration
        self._buffer: list[tuple[str, Any]] = []
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
        batch_interval: timedelta = timedelta(seconds=2),
        max_batch_size: int | None = None,
        max_retry_duration: timedelta = timedelta(seconds=600),
    ) -> WorkflowStreamClient:
        """Create a stream client from a Temporal client and workflow ID.

        Use this when the caller has an explicit ``Client`` and
        ``workflow_id`` in hand (starters, BFFs, other workflows'
        activities). For code running inside an activity that targets
        its own parent workflow, see :py:meth:`from_activity`.

        A client created through this method follows continue-as-new
        chains in ``subscribe()`` and uses the client's payload
        converter for per-item ``Payload`` construction.

        Args:
            client: Temporal client.
            workflow_id: ID of the workflow hosting the stream.
            batch_interval: Interval between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum time to retry a failed flush
                before raising TimeoutError. Default: 10 minutes.
        """
        handle = client.get_workflow_handle(workflow_id)
        return cls(
            handle,
            client=client,
            batch_interval=batch_interval,
            max_batch_size=max_batch_size,
            max_retry_duration=max_retry_duration,
        )

    @classmethod
    def from_activity(
        cls,
        *,
        batch_interval: timedelta = timedelta(seconds=2),
        max_batch_size: int | None = None,
        max_retry_duration: timedelta = timedelta(seconds=600),
    ) -> WorkflowStreamClient:
        """Create a stream client targeting the current activity's parent workflow.

        Must be called from within an activity that was scheduled by a
        workflow. The Temporal client and parent workflow id are taken
        from the activity context.

        Standalone activities — those started directly via
        :py:meth:`temporalio.client.Client.start_activity` rather than
        from a workflow — have no parent workflow, so this method
        raises. Use :py:meth:`create` from a standalone activity,
        passing ``activity.client()`` and the target workflow id
        explicitly (typically threaded through the activity's input).

        Args:
            batch_interval: Interval between automatic flushes.
            max_batch_size: Auto-flush when buffer reaches this size.
            max_retry_duration: Maximum time to retry a failed flush
                before raising TimeoutError. Default: 10 minutes.
        """
        info = activity.info()
        workflow_id = info.workflow_id
        if workflow_id is None:
            raise RuntimeError(
                "from_activity requires an activity scheduled by a workflow; "
                "this activity has no parent workflow. From a standalone "
                "activity, use WorkflowStreamClient.create(activity.client(), "
                "workflow_id) with the target workflow id passed in explicitly."
            )
        return cls.create(
            activity.client(),
            workflow_id,
            batch_interval=batch_interval,
            max_batch_size=max_batch_size,
            max_retry_duration=max_retry_duration,
        )

    async def __aenter__(self) -> Self:
        """Start the background flusher task."""
        self._flush_task = asyncio.create_task(self._run_flusher())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop the flusher and flush any remaining buffered entries."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        # Drain both pending and buffer. A single _flush() processes
        # either pending OR buffer, not both — so if the flusher was
        # cancelled mid-signal (pending set) while the producer added
        # more items (buffer non-empty), a single final flush would
        # orphan the buffer.
        while self._pending is not None or self._buffer:
            await self._flush()

    def publish(self, topic: str, value: Any, force_flush: bool = False) -> None:
        """Buffer a message for publishing.

        ``value`` may be any Python value the client's payload
        converter can handle, or a pre-built
        :class:`temporalio.api.common.v1.Payload` for zero-copy. The
        codec chain is not applied per item — it runs once on the
        signal envelope that delivers the batch.

        Args:
            topic: Topic string.
            value: Value to publish. Converted to a ``Payload`` via
                the client's sync payload converter at flush time.
                Pre-built ``Payload`` instances bypass conversion.
            force_flush: If True, wake the flusher to send immediately
                (fire-and-forget — does not block the caller).
        """
        self._buffer.append((topic, value))
        if force_flush or (
            self._max_batch_size is not None
            and len(self._buffer) >= self._max_batch_size
        ):
            self._flush_event.set()

    async def flush(self) -> None:
        """Flush buffered (and pending) items and wait for server confirmation.

        Returns once the items buffered at call time have been signaled to
        the workflow and acknowledged by the server. Returns immediately
        if there is nothing to send.

        This is in addition to the declarative ``force_flush=True`` on
        :py:meth:`publish` and to the automatic flush on context-manager
        exit. Use this when you need a synchronization point — proof
        that prior publications have reached the server — at a moment
        that does not naturally correspond to a specific event.

        Safe to call concurrently with ``publish()`` and with the
        background flusher: the flush lock serializes signal sends.
        Items added concurrently after entry may piggyback on this
        flush or be deferred to a subsequent one.

        Raises:
            TimeoutError: If a pending batch from a prior failure cannot
                be sent within ``max_retry_duration``. The pending batch
                is dropped; subsequent publications use a fresh sequence.
        """
        while self._pending is not None or self._buffer:
            await self._flush()

    def _payload_converter(self) -> PayloadConverter:
        """Return the sync payload converter for per-item encode/decode.

        Uses the configured client's payload converter when available;
        otherwise falls back to the default. The codec chain
        (e.g. encryption, compression) is intentionally not
        invoked here — it runs once at the envelope level when the
        signal/update goes over the wire. See module docstring.
        """
        if self._client is not None:
            return self._client.data_converter.payload_converter
        return DataConverter.default.payload_converter

    def _encode_buffer(self, entries: list[tuple[str, Any]]) -> list[PublishEntry]:
        """Convert buffered (topic, value) pairs to wire entries.

        Non-Payload values go through the sync payload converter so the
        resulting ``Payload`` carries encoding metadata for
        ``result_type=`` decode on the consumer side. Pre-built
        Payloads bypass conversion.
        """
        converter = self._payload_converter()
        out: list[PublishEntry] = []
        for topic, value in entries:
            if isinstance(value, Payload):
                payload = value
            else:
                payload = converter.to_payloads([value])[0]
            out.append(PublishEntry(topic=topic, data=_encode_payload(payload)))
        return out

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
                    > self._max_retry_duration.total_seconds()
                ):
                    # Advance confirmed sequence so the next batch gets
                    # a fresh sequence number. Without this, the next
                    # batch reuses pending_seq, which the workflow may
                    # have already accepted — causing silent dedup
                    # (data loss). See DropPendingFixed /
                    # SequenceFreshness in the design doc.
                    self._sequence = self._pending_seq
                    self._pending = None
                    self._pending_seq = 0
                    self._pending_since = None
                    raise TimeoutError(
                        f"Flush retry exceeded max_retry_duration "
                        f"({self._max_retry_duration}). Pending batch dropped. "
                        f"If the signal was delivered, items are in the log. "
                        f"If not, they are lost."
                    )
                batch = self._pending
                seq = self._pending_seq
            elif self._buffer:
                # New batch path. Encode before clearing the buffer so
                # a payload-converter exception leaves the items in
                # place for inspection or retry rather than silently
                # dropping them.
                batch = self._encode_buffer(self._buffer)
                self._buffer = []
                seq = self._sequence + 1
                self._pending = batch
                self._pending_seq = seq
                self._pending_since = time.monotonic()
            else:
                return

            try:
                # If the SDK ever exposes request_id on signal() and the
                # server dedups it across CAN, pinning
                # request_id=f"{publisher_id}:{seq}" here lets the
                # workflow-side dedup go away. See DESIGN §"Replace
                # workflow-side dedup with server-side request_id".
                await self._handle.signal(
                    "__temporal_workflow_stream_publish",
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
                    self._flush_event.wait(),
                    timeout=self._batch_interval.total_seconds(),
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            await self._flush()

    async def subscribe(
        self,
        topics: str | list[str] | None = None,
        from_offset: int = 0,
        *,
        result_type: type | None = None,
        poll_cooldown: timedelta = timedelta(milliseconds=100),
    ) -> AsyncIterator[WorkflowStreamItem]:
        """Async iterator that polls for new items.

        Automatically follows continue-as-new chains when the client
        was created via :py:meth:`create`.

        Args:
            topics: Topic filter. A single topic name, a list of topic
                names, or None. None or empty list means all topics.
            from_offset: Global offset to start reading from.
            result_type: Optional target type. When provided, each
                yielded :class:`WorkflowStreamItem` has its ``data``
                decoded via the client's sync payload converter to the
                specified type. When omitted, ``data`` is the raw
                ``temporalio.api.common.v1.Payload`` — useful for
                heterogeneous topics where the caller dispatches on
                ``Payload.metadata``.
            poll_cooldown: Minimum interval between polls to avoid
                overwhelming the workflow when items arrive faster
                than the poll round-trip. Defaults to 100ms.

        Yields:
            :class:`WorkflowStreamItem` for each matching item.
        """
        topic_filter: list[str]
        if topics is None:
            topic_filter = []
        elif isinstance(topics, str):
            topic_filter = [topics]
        else:
            topic_filter = topics
        offset = from_offset
        while True:
            try:
                result: PollResult = await self._handle.execute_update(
                    "__temporal_workflow_stream_poll",
                    PollInput(topics=topic_filter, from_offset=offset),
                    result_type=PollResult,
                )
            except asyncio.CancelledError:
                return
            except WorkflowUpdateFailedError as e:
                if e.cause and getattr(e.cause, "type", None) == "TruncatedOffset":
                    # Subscriber fell behind truncation. Retry from
                    # offset 0 which the stream treats as "from the
                    # beginning of whatever exists" (i.e., from
                    # base_offset).
                    offset = 0
                    continue
                raise
            except WorkflowUpdateRPCTimeoutOrCancelledError:
                if await self._follow_continue_as_new():
                    continue
                return
            converter = self._payload_converter()
            for wire_item in result.items:
                payload = _decode_payload(wire_item.data)
                if result_type is not None:
                    data: Any = converter.from_payload(payload, result_type)
                else:
                    data = payload
                yield WorkflowStreamItem(
                    topic=wire_item.topic,
                    data=data,
                    offset=wire_item.offset,
                )
            offset = result.next_offset
            cooldown_secs = poll_cooldown.total_seconds()
            if not result.more_ready and cooldown_secs > 0:
                await asyncio.sleep(cooldown_secs)

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
        return await self._handle.query(
            "__temporal_workflow_stream_offset", result_type=int
        )
