"""Workflow-side pub/sub broker.

Instantiate :class:`PubSub` once from your workflow's ``@workflow.init``
method. The constructor registers the pub/sub signal, update, and query
handlers on the current workflow via
:func:`temporalio.workflow.set_signal_handler`,
:func:`temporalio.workflow.set_update_handler`, and
:func:`temporalio.workflow.set_query_handler`.

For workflows that support continue-as-new, include a
``PubSubState | None`` field on the workflow input and pass it as
``prior_state`` — it is ``None`` on fresh starts and carries accumulated
state on continue-as-new.

Both workflow-side :meth:`PubSub.publish` and client-side
:meth:`PubSubClient.publish` use the synchronous payload converter for
per-item ``Payload`` construction. The codec chain (encryption,
PII-redaction, compression) is **not** run per item on either side —
it runs once at the envelope level when Temporal's SDK encodes the
signal/update that carries the batch. Running it per item as well
would double-encrypt, because every signal arg already goes through
the client's ``DataConverter.encode`` at dispatch time.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, NoReturn, Sequence

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError

from ._types import (
    PollInput,
    PollResult,
    PublishInput,
    PubSubItem,
    PubSubState,
    _decode_payload,
    _encode_payload,
    _WireItem,
)

_PUBLISH_SIGNAL = "__temporal_pubsub_publish"
_POLL_UPDATE = "__temporal_pubsub_poll"
_OFFSET_QUERY = "__temporal_pubsub_offset"

_MAX_POLL_RESPONSE_BYTES = 1_000_000


def _payload_wire_size(payload: Payload, topic: str) -> int:
    """Approximate poll-response contribution of a single item.

    Wire form is ``_WireItem(topic, base64(proto(Payload)), offset)``.
    Base64 inflates by ~4/3; we use the exact serialized length as a
    close-enough proxy.
    """
    return (payload.ByteSize() * 4 + 2) // 3 + len(topic)


class PubSub:
    """Workflow-side pub/sub broker.

    Construct once from ``@workflow.init``; the constructor registers
    the pub/sub signal, update, and query handlers on the current
    workflow. Raises :class:`RuntimeError` if a ``PubSub`` has already
    been registered on the workflow.

    Registered handlers:

    - ``__temporal_pubsub_publish`` signal — external publish with dedup
    - ``__temporal_pubsub_poll`` update — long-poll subscription
    - ``__temporal_pubsub_offset`` query — current log length

    Note:
        Because ``__temporal_pubsub_publish`` is registered *dynamically* from
        ``__init__``, custom **synchronous** update/signal handlers
        that read ``PubSub`` state can observe pre-publish state when
        both land in the same activation. Make such handlers ``async``
        and ``await asyncio.sleep(0)`` before reading state. See the
        "Gotcha" section of this module's ``README.md`` for the
        full explanation and recipe.
    """

    def __init__(self, prior_state: PubSubState | None = None) -> None:
        """Initialize pub/sub state and register workflow handlers.

        Must be called directly from the workflow's ``@workflow.init``
        method. Calls made from ``@workflow.run``, helper methods, or
        signal/update/query handlers raise :class:`RuntimeError`.

        The check inspects the immediate caller's frame and requires the
        function name to be ``__init__``. A history-length check (expect
        length 3 on the first workflow task) is not used because
        pre-start signals inflate the first-task history and cache
        evictions legitimately re-run ``__init__`` from later tasks.

        Args:
            prior_state: State carried from a previous run via
                :meth:`get_state` through continue-as-new, or ``None``
                on first start.

        Raises:
            RuntimeError: If not called directly from a method named
                ``__init__``, or if the pub/sub signal handler is
                already registered on this workflow (i.e., ``PubSub``
                was instantiated twice).

        Note:
            When carrying state across continue-as-new, type the
            carrying field as ``PubSubState | None`` — not ``Any``. The
            default data converter deserializes ``Any`` fields as plain
            dicts, which silently strips the ``PubSubState`` type and
            breaks the new run.
        """
        caller = sys._getframe(1)
        caller_name = caller.f_code.co_name
        if caller_name != "__init__":
            raise RuntimeError(
                "PubSub must be constructed directly from the workflow's "
                f"@workflow.init method, not from {caller_name!r}."
            )
        if workflow.get_signal_handler(_PUBLISH_SIGNAL) is not None:
            raise RuntimeError(
                "PubSub is already registered on this workflow. "
                "Construct PubSub(...) at most once from @workflow.init."
            )

        if prior_state is not None:
            self._log: list[PubSubItem] = [
                PubSubItem(topic=item.topic, data=_decode_payload(item.data))
                for item in prior_state.log
            ]
            self._base_offset: int = prior_state.base_offset
            self._publisher_sequences: dict[str, int] = dict(
                prior_state.publisher_sequences
            )
            self._publisher_last_seen: dict[str, float] = dict(
                prior_state.publisher_last_seen
            )
        else:
            self._log = []
            self._base_offset = 0
            self._publisher_sequences = {}
            self._publisher_last_seen = {}
        self._draining: bool = False

        workflow.set_signal_handler(_PUBLISH_SIGNAL, self._on_publish)
        workflow.set_update_handler(
            _POLL_UPDATE, self._on_poll, validator=self._validate_poll
        )
        workflow.set_query_handler(_OFFSET_QUERY, self._on_offset)

    def publish(self, topic: str, value: Any) -> None:
        """Publish an item from within workflow code.

        ``value`` may be any Python value the workflow's payload
        converter can handle, or a pre-built
        :class:`temporalio.api.common.v1.Payload` for zero-copy.

        The codec chain is not applied here (it runs on the
        ``__temporal_pubsub_poll`` update envelope that later delivers the
        item to a subscriber).
        """
        if isinstance(value, Payload):
            payload = value
        else:
            payload = workflow.payload_converter().to_payloads([value])[0]
        self._log.append(PubSubItem(topic=topic, data=payload))

    def get_state(self, *, publisher_ttl: float = 900.0) -> PubSubState:
        """Return a serializable snapshot of pub/sub state for continue-as-new.

        Prunes publisher dedup entries older than ``publisher_ttl``
        seconds. The TTL must exceed the ``max_retry_duration`` of any
        client that may still be retrying a failed flush.

        Args:
            publisher_ttl: Seconds after which a publisher's dedup
                entry is pruned. Default 900 (15 minutes).
        """
        now = workflow.time()

        active_sequences: dict[str, int] = {}
        active_last_seen: dict[str, float] = {}
        for pid, seq in self._publisher_sequences.items():
            ts = self._publisher_last_seen.get(pid, 0.0)
            if now - ts < publisher_ttl:
                active_sequences[pid] = seq
                active_last_seen[pid] = ts

        return PubSubState(
            log=[
                _WireItem(topic=item.topic, data=_encode_payload(item.data))
                for item in self._log
            ],
            base_offset=self._base_offset,
            publisher_sequences=active_sequences,
            publisher_last_seen=active_last_seen,
        )

    def drain(self) -> None:
        """Unblock all waiting poll handlers and reject new polls.

        Call this before
        ``await workflow.wait_condition(workflow.all_handlers_finished)``
        and ``workflow.continue_as_new()``.
        """
        self._draining = True

    async def continue_as_new(
        self,
        build_args: Callable[[PubSubState], Sequence[Any]],
        *,
        publisher_ttl: float = 900.0,
    ) -> NoReturn:
        """Drain, wait for handlers, then continue-as-new with built args.

        Replaces the three-line recipe ``drain()`` →
        ``wait_condition(all_handlers_finished)`` →
        ``workflow.continue_as_new(args=...)`` for the common case where
        the only CAN parameter that varies is ``args``.

        ``build_args`` is invoked *after* drain has stabilized, with the
        post-drain :class:`PubSubState` as its single argument. The
        caller threads that state into whatever input dataclass the
        workflow expects:

        .. code-block:: python

            await self.pubsub.continue_as_new(lambda state: [WorkflowInput(
                items_processed=self.items_processed,
                pubsub_state=state,
            )])

        Workflows that need to override other CAN parameters
        (``task_queue``, ``retry_policy``, ``run_timeout``, etc.) should
        keep using the explicit ``drain`` / ``wait_condition`` /
        ``workflow.continue_as_new(...)`` recipe.

        Args:
            build_args: Callable that receives the post-drain pub/sub
                state and returns the positional ``args`` for the new
                run.
            publisher_ttl: Forwarded to :meth:`get_state`.

        Does not return; ``workflow.continue_as_new`` raises an internal
        exception that the SDK uses to close the run.
        """
        self.drain()
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(
            args=build_args(self.get_state(publisher_ttl=publisher_ttl)),
        )

    def truncate(self, up_to_offset: int) -> None:
        """Discard log entries before ``up_to_offset``.

        After truncation, polls requesting an offset before the new
        base will receive an ApplicationError. All global offsets
        remain monotonic.

        Raises ApplicationError (not ValueError) when ``up_to_offset``
        is past the end of the log so that callers invoking this from
        an update handler surface it as an update failure rather than
        a workflow-task poison pill.

        Args:
            up_to_offset: The global offset to truncate up to
                (exclusive). Entries at offsets
                ``[base_offset, up_to_offset)`` are discarded.
        """
        log_index = up_to_offset - self._base_offset
        if log_index <= 0:
            return
        if log_index > len(self._log):
            raise ApplicationError(
                f"Cannot truncate to offset {up_to_offset}: only "
                f"{self._base_offset + len(self._log)} items exist",
                type="TruncateOutOfRange",
                non_retryable=True,
            )
        self._log = self._log[log_index:]
        self._base_offset = up_to_offset

    def _on_publish(self, payload: PublishInput) -> None:
        """Receive publications from external clients (activities, starters).

        Deduplicates using (publisher_id, sequence). If publisher_id is
        set and the sequence is <= the last seen sequence for that
        publisher, the entire batch is dropped as a duplicate. Batches
        are atomic: the dedup decision applies to the whole batch, not
        individual items.

        This block is a polyfill for missing server-side ``request_id``
        dedup across continue-as-new. If the SDK ever exposes
        ``request_id`` on signals and the server dedups it across CAN,
        this branch and the ``_publisher_sequences`` /
        ``_publisher_last_seen`` state become redundant. See DESIGN-v2
        §"Replace workflow-side dedup with server-side request_id" for
        the migration plan.
        """
        if payload.publisher_id:
            last_seq = self._publisher_sequences.get(payload.publisher_id, 0)
            if payload.sequence <= last_seq:
                return
            self._publisher_sequences[payload.publisher_id] = payload.sequence
            self._publisher_last_seen[payload.publisher_id] = workflow.time()
        for entry in payload.items:
            self._log.append(
                PubSubItem(topic=entry.topic, data=_decode_payload(entry.data))
            )

    async def _on_poll(self, payload: PollInput) -> PollResult:
        """Long-poll: block until new items available or draining, then return."""
        log_offset = payload.from_offset - self._base_offset
        if log_offset < 0:
            if payload.from_offset == 0:
                # "From the beginning" — start at whatever is available.
                log_offset = 0
            else:
                # Subscriber had a specific position that's been
                # truncated. ApplicationError fails this update (client
                # gets the error) without crashing the workflow task —
                # avoids a poison pill during replay.
                raise ApplicationError(
                    f"Requested offset {payload.from_offset} has been truncated. "
                    f"Current base offset is {self._base_offset}.",
                    type="TruncatedOffset",
                    non_retryable=True,
                )
        await workflow.wait_condition(
            lambda: len(self._log) > log_offset or self._draining,
        )
        all_new = self._log[log_offset:]
        if payload.topics:
            topic_set = set(payload.topics)
            candidates = [
                (self._base_offset + log_offset + i, item)
                for i, item in enumerate(all_new)
                if item.topic in topic_set
            ]
        else:
            candidates = [
                (self._base_offset + log_offset + i, item)
                for i, item in enumerate(all_new)
            ]
        # Cap response size to ~1MB wire bytes.
        wire_items: list[_WireItem] = []
        size = 0
        more_ready = False
        next_offset = self._base_offset + len(self._log)
        for off, item in candidates:
            item_size = _payload_wire_size(item.data, item.topic)
            if size + item_size > _MAX_POLL_RESPONSE_BYTES and wire_items:
                # Resume from this item on the next poll.
                next_offset = off
                more_ready = True
                break
            size += item_size
            wire_items.append(
                _WireItem(topic=item.topic, data=_encode_payload(item.data), offset=off)
            )
        return PollResult(
            items=wire_items,
            next_offset=next_offset,
            more_ready=more_ready,
        )

    def _validate_poll(self, _payload: PollInput) -> None:
        """Reject new polls when draining for continue-as-new."""
        if self._draining:
            raise RuntimeError("Workflow is draining for continue-as-new")

    def _on_offset(self) -> int:
        """Return the current global offset (base_offset + log length)."""
        return self._base_offset + len(self._log)
