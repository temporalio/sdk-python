"""Workflow-side pub/sub mixin.

Add PubSubMixin as a base class to any workflow to get pub/sub signal, update,
and query handlers.

Call ``init_pubsub()`` in ``__init__`` for fresh workflows, or in ``run()``
when accepting ``prior_state`` from continue-as-new arguments.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from ._types import (
    PollInput,
    PollResult,
    PubSubItem,
    PubSubState,
    PublishInput,
    _WireItem,
    decode_data,
    encode_data,
)


_MAX_POLL_RESPONSE_BYTES = 1_000_000


class PubSubMixin:
    """Mixin that turns a workflow into a pub/sub broker.

    Provides:
    - ``publish(topic, data)`` for workflow-side publishing
    - ``__pubsub_publish`` signal for external publishing (with dedup)
    - ``__pubsub_poll`` update for long-poll subscription
    - ``__pubsub_offset`` query for current log length
    - ``drain_pubsub()`` / ``get_pubsub_state()`` for continue-as-new
    - ``truncate_pubsub(offset)`` for log prefix truncation
    """

    _pubsub_log: list[PubSubItem]
    _pubsub_base_offset: int
    _pubsub_publisher_sequences: dict[str, int]
    _pubsub_publisher_last_seen: dict[str, float]
    _pubsub_draining: bool

    def init_pubsub(self, prior_state: PubSubState | None = None) -> None:
        """Initialize pub/sub state.

        Args:
            prior_state: State carried from a previous run via
                ``get_pubsub_state()`` through continue-as-new. Pass None
                on the first run.
        """
        if prior_state is not None:
            self._pubsub_log = [
                PubSubItem(topic=item.topic, data=decode_data(item.data))
                for item in prior_state.log
            ]
            self._pubsub_base_offset = prior_state.base_offset
            self._pubsub_publisher_sequences = dict(
                prior_state.publisher_sequences
            )
            self._pubsub_publisher_last_seen = dict(
                prior_state.publisher_last_seen
            )
        else:
            self._pubsub_log = []
            self._pubsub_base_offset = 0
            self._pubsub_publisher_sequences = {}
            self._pubsub_publisher_last_seen = {}
        self._pubsub_draining = False

    def get_pubsub_state(
        self, *, publisher_ttl: float = 900.0
    ) -> PubSubState:
        """Return a serializable snapshot of pub/sub state for continue-as-new.

        Prunes publisher dedup entries older than ``publisher_ttl`` seconds.
        The TTL must exceed the ``max_retry_duration`` of any client that
        may still be retrying a failed flush.

        Args:
            publisher_ttl: Seconds after which a publisher's dedup entry
                is pruned. Default 900 (15 minutes).
        """
        self._check_initialized()
        now = workflow.time()

        # Prune publishers whose last activity exceeds the TTL.
        active_sequences: dict[str, int] = {}
        active_last_seen: dict[str, float] = {}
        for pid, seq in self._pubsub_publisher_sequences.items():
            ts = self._pubsub_publisher_last_seen.get(pid, 0.0)
            if now - ts < publisher_ttl:
                active_sequences[pid] = seq
                active_last_seen[pid] = ts

        return PubSubState(
            log=[
                _WireItem(topic=item.topic, data=encode_data(item.data))
                for item in self._pubsub_log
            ],
            base_offset=self._pubsub_base_offset,
            publisher_sequences=active_sequences,
            publisher_last_seen=active_last_seen,
        )

    def drain_pubsub(self) -> None:
        """Unblock all waiting poll handlers and reject new polls.

        Call this before ``await workflow.wait_condition(workflow.all_handlers_finished)``
        and ``workflow.continue_as_new()``.
        """
        self._check_initialized()
        self._pubsub_draining = True

    def truncate_pubsub(self, up_to_offset: int) -> None:
        """Discard log entries before ``up_to_offset``.

        After truncation, polls requesting an offset before the new
        base will receive a ValueError. All global offsets remain
        monotonic.

        Args:
            up_to_offset: The global offset to truncate up to (exclusive).
                Entries at offsets ``[base_offset, up_to_offset)`` are
                discarded.
        """
        self._check_initialized()
        log_index = up_to_offset - self._pubsub_base_offset
        if log_index <= 0:
            return
        if log_index > len(self._pubsub_log):
            raise ValueError(
                f"Cannot truncate to offset {up_to_offset}: "
                f"only {self._pubsub_base_offset + len(self._pubsub_log)} "
                f"items exist"
            )
        self._pubsub_log = self._pubsub_log[log_index:]
        self._pubsub_base_offset = up_to_offset

    def _check_initialized(self) -> None:
        if not hasattr(self, "_pubsub_log"):
            raise RuntimeError(
                "PubSubMixin not initialized. Call self.init_pubsub() in "
                "your workflow's __init__ or at the start of run()."
            )

    def publish(self, topic: str, data: bytes) -> None:
        """Publish an item from within workflow code. Deterministic — just appends."""
        self._check_initialized()
        self._pubsub_log.append(PubSubItem(topic=topic, data=data))

    @workflow.signal(name="__pubsub_publish")
    def _pubsub_publish(self, input: PublishInput) -> None:
        """Receive publications from external clients (activities, starters).

        Deduplicates using (publisher_id, sequence). If publisher_id is set
        and the sequence is <= the last seen sequence for that publisher,
        the entire batch is dropped as a duplicate. Batches are atomic:
        the dedup decision applies to the whole batch, not individual items.
        """
        self._check_initialized()
        if input.publisher_id:
            last_seq = self._pubsub_publisher_sequences.get(
                input.publisher_id, 0
            )
            if input.sequence <= last_seq:
                return
            self._pubsub_publisher_sequences[input.publisher_id] = (
                input.sequence
            )
            self._pubsub_publisher_last_seen[input.publisher_id] = (
                workflow.time()
            )
        for entry in input.items:
            self._pubsub_log.append(
                PubSubItem(topic=entry.topic, data=decode_data(entry.data))
            )

    @workflow.update(name="__pubsub_poll")
    async def _pubsub_poll(self, input: PollInput) -> PollResult:
        """Long-poll: block until new items available or draining, then return."""
        self._check_initialized()
        log_offset = input.from_offset - self._pubsub_base_offset
        if log_offset < 0:
            if input.from_offset == 0:
                # "From the beginning" — start at whatever is available.
                log_offset = 0
            else:
                # Subscriber had a specific position that's been truncated.
                # ApplicationError fails this update (client gets the error)
                # without crashing the workflow task — avoids a poison pill
                # during replay.
                raise ApplicationError(
                    f"Requested offset {input.from_offset} has been truncated. "
                    f"Current base offset is {self._pubsub_base_offset}.",
                    type="TruncatedOffset",
                    non_retryable=True,
                )
        await workflow.wait_condition(
            lambda: len(self._pubsub_log) > log_offset
            or self._pubsub_draining,
        )
        all_new = self._pubsub_log[log_offset:]
        if input.topics:
            topic_set = set(input.topics)
            candidates = [
                (self._pubsub_base_offset + log_offset + i, item)
                for i, item in enumerate(all_new)
                if item.topic in topic_set
            ]
        else:
            candidates = [
                (self._pubsub_base_offset + log_offset + i, item)
                for i, item in enumerate(all_new)
            ]
        # Cap response size to ~1MB estimated wire bytes.
        wire_items: list[_WireItem] = []
        size = 0
        more_ready = False
        next_offset = self._pubsub_base_offset + len(self._pubsub_log)
        for off, item in candidates:
            encoded = encode_data(item.data)
            item_size = len(encoded) + len(item.topic)
            if size + item_size > _MAX_POLL_RESPONSE_BYTES and wire_items:
                # Resume from this item on the next poll.
                next_offset = off
                more_ready = True
                break
            size += item_size
            wire_items.append(
                _WireItem(topic=item.topic, data=encoded, offset=off)
            )
        return PollResult(
            items=wire_items,
            next_offset=next_offset,
            more_ready=more_ready,
        )

    @_pubsub_poll.validator
    def _validate_pubsub_poll(self, input: PollInput) -> None:  # noqa: A002
        """Reject new polls when draining for continue-as-new."""
        self._check_initialized()
        if self._pubsub_draining:
            raise RuntimeError("Workflow is draining for continue-as-new")

    @workflow.query(name="__pubsub_offset")
    def _pubsub_offset(self) -> int:
        """Return the current global offset (base_offset + log length)."""
        self._check_initialized()
        return self._pubsub_base_offset + len(self._pubsub_log)
