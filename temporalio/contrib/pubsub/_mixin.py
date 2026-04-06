"""Workflow-side pub/sub mixin.

Add PubSubMixin as a base class to any workflow to get pub/sub signal, update,
and query handlers. Call ``init_pubsub()`` in your workflow's ``__init__`` or
at the start of ``run()``.
"""

from __future__ import annotations

from temporalio import workflow

from ._types import PollInput, PollResult, PubSubItem, PubSubState, PublishInput


class PubSubMixin:
    """Mixin that turns a workflow into a pub/sub broker.

    Provides:
    - ``publish(topic, data)`` for workflow-side publishing
    - ``__pubsub_publish`` signal for external publishing
    - ``__pubsub_poll`` update for long-poll subscription
    - ``__pubsub_offset`` query for current log length
    - ``drain_pubsub()`` / ``get_pubsub_state()`` for continue-as-new
    """

    def init_pubsub(self, prior_state: PubSubState | None = None) -> None:
        """Initialize pub/sub state.

        Args:
            prior_state: State from a previous run (via get_pubsub_state()).
                         Pass None on the first run.
        """
        if prior_state is not None:
            self._pubsub_log: list[PubSubItem] = list(prior_state.log)
        else:
            self._pubsub_log = []
        self._pubsub_draining = False

    def get_pubsub_state(self) -> PubSubState:
        """Return a serializable snapshot of pub/sub state for continue-as-new."""
        return PubSubState(log=list(self._pubsub_log))

    def drain_pubsub(self) -> None:
        """Unblock all waiting poll handlers and reject new polls.

        Call this before ``await workflow.wait_condition(workflow.all_handlers_finished)``
        and ``workflow.continue_as_new()``.
        """
        self._pubsub_draining = True

    def publish(self, topic: str, data: bytes) -> None:
        """Publish an item from within workflow code. Deterministic — just appends."""
        offset = len(self._pubsub_log)
        self._pubsub_log.append(PubSubItem(offset=offset, topic=topic, data=data))

    @workflow.signal(name="__pubsub_publish")
    def _pubsub_publish(self, input: PublishInput) -> None:
        """Receive publications from external clients (activities, starters)."""
        for entry in input.items:
            offset = len(self._pubsub_log)
            self._pubsub_log.append(
                PubSubItem(offset=offset, topic=entry.topic, data=entry.data)
            )

    @workflow.update(name="__pubsub_poll")
    async def _pubsub_poll(self, input: PollInput) -> PollResult:
        """Long-poll: block until new items available or draining, then return."""
        await workflow.wait_condition(
            lambda: len(self._pubsub_log) > input.from_offset
            or self._pubsub_draining,
            timeout=input.timeout,
        )
        all_new = self._pubsub_log[input.from_offset :]
        next_offset = len(self._pubsub_log)
        if input.topics:
            topic_set = set(input.topics)
            filtered = [item for item in all_new if item.topic in topic_set]
        else:
            filtered = list(all_new)
        return PollResult(items=filtered, next_offset=next_offset)

    @_pubsub_poll.validator
    def _validate_pubsub_poll(self, input: PollInput) -> None:
        if self._pubsub_draining:
            raise RuntimeError("Workflow is draining for continue-as-new")

    @workflow.query(name="__pubsub_offset")
    def _pubsub_offset(self) -> int:
        """Return the current log length (next offset)."""
        return len(self._pubsub_log)
