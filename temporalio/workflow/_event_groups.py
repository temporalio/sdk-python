"""Event Groups, a way to regroup logically related workflow events.

.. warning::
    Event Groups is an experimental API and may change without notice.
"""

from __future__ import annotations

import contextvars
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import temporalio.api.sdk.v1
import temporalio.converter

from ._context import _Runtime

__all__ = [
    "EventGroup",
    "create_event_group",
]


class EventGroup(ABC):
    """A discrete token associating workflow commands, and the history events
    they produce, with a logical group for UI and observability purposes.

    Multiple Event Groups may be attached to a single command, and a single
    Event Group may be attached to multiple commands.

    Instances are created with :py:func:`create_event_group`. They may be
    attached to specific commands via the ``event_groups`` option of the API
    producing the command, or to every command produced within a block of
    workflow code via :py:meth:`scope`.

    .. warning::
        Event Groups is an experimental API and may change without notice.
    """

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Context manager attaching this Event Group to every command produced
        within it.

        Scopes nest and compose: a command produced inside an inner scope
        carries the Event Groups of all enclosing scopes. Coroutines started
        within a scope inherit it, since they capture the context active at
        their creation.

        Only usable from within a workflow.

        .. warning::
            Event Groups is an experimental API and may change without notice.
        """
        _Runtime.current()
        token = _active_event_groups.set(self._applied_over(_active_event_groups.get()))
        try:
            yield
        finally:
            try:
                _active_event_groups.reset(token)
            except ValueError:
                # Unwinding from a context other than the one the scope was
                # entered in, which happens when a coroutine suspended inside
                # the scope is closed rather than resumed. The context the
                # value was set in is being discarded anyway.
                pass

    @abstractmethod
    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        """Return the active set resulting from entering this group's scope."""
        ...

    @abstractmethod
    def _to_proto(
        self, payload_converter: temporalio.converter.PayloadConverter
    ) -> temporalio.api.sdk.v1.EventGroupMarker:
        """Serialize as the marker attached to a workflow command."""
        ...


class _LabelEventGroup(EventGroup):
    """An Event Group explicitly created by workflow code."""

    def __init__(self, id: str, label: str) -> None:
        self._id = id
        self._label = label

    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        return _ActiveEventGroups(
            implicit=active.implicit,
            explicit=_with_group(active.explicit, self),
        )

    def _to_proto(
        self, payload_converter: temporalio.converter.PayloadConverter
    ) -> temporalio.api.sdk.v1.EventGroupMarker:
        return temporalio.api.sdk.v1.EventGroupMarker(
            label=temporalio.api.sdk.v1.EventGroupMarker.Label(
                id=self._id,
                label=payload_converter.to_payload(self._label),
            )
        )


class _ImplicitEventGroup(EventGroup):
    """An Event Group created by the SDK around an inbound workflow event."""

    def __init__(self, marker: temporalio.api.sdk.v1.EventGroupMarker) -> None:
        self._marker = marker

    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        # Implicit groups intentionally do not inherit the enclosing scope: a
        # handler registered inside an explicit scope must not attribute its
        # commands to that scope.
        return _ActiveEventGroups(implicit=self)

    def _to_proto(
        self, payload_converter: temporalio.converter.PayloadConverter
    ) -> temporalio.api.sdk.v1.EventGroupMarker:
        return self._marker


@dataclass(frozen=True)
class _ActiveEventGroups:
    implicit: EventGroup | None = None
    explicit: tuple[_LabelEventGroup, ...] = ()


_active_event_groups: contextvars.ContextVar[_ActiveEventGroups] = (
    contextvars.ContextVar(
        "__temporal_active_event_groups", default=_ActiveEventGroups()
    )
)


def create_event_group(label: str, *, id: str | None = None) -> EventGroup:
    """Create an Event Group that can be attached to commands produced by this
    workflow.

    Args:
        label: User-visible label for the group, surfaced in the UI and CLI.
            The label is encoded using the worker's configured payload codecs.

            Note that when no ``id`` is given, the id is derived from the label
            using a hash function. Given short and predictable labels,
            brute-forcing the hashed value may be computationally feasible,
            thereby recovering the label. Avoid putting sensitive information
            in labels, or provide an explicit ``id``.
        id: Opaque identifier determining whether two Event Groups are the
            same. Events are grouped together if and only if their groups have
            the same id, without regard to their labels; only the first label
            seen for a given id is used. Defaults to a deterministic,
            replay-stable value derived from the label. The id is not encoded
            using payload codecs.

    Returns:
        The new Event Group.

    .. warning::
        Event Groups is an experimental API and may change without notice.
    """
    info = _Runtime.current().workflow_info()
    if not label:
        raise ValueError("Event group label cannot be empty")
    if id is None:
        # Salted with the run id so that the label cannot be recovered from the
        # id using precomputed hashes. This is the run id of the
        # WorkflowExecutionStarted event, which is preserved across resets, so
        # ids remain stable on replay and after a reset.
        id = hashlib.sha1(
            f"{info.original_execution_run_id}{label}".encode()
        ).hexdigest()
    elif not id:
        raise ValueError("Event group id cannot be empty")
    return _LabelEventGroup(id, label)


def _inbound_event_group(event_id: int) -> EventGroup:
    """Create the implicit Event Group for an inbound history event."""
    if event_id <= 0:
        raise ValueError(f"Invalid inbound event id: {event_id}")
    return _ImplicitEventGroup(
        temporalio.api.sdk.v1.EventGroupMarker(
            inbound_event=temporalio.api.sdk.v1.EventGroupMarker.InboundEvent(
                inbound_event_id=event_id
            )
        )
    )


def _inbound_update_event_group(update_id: str) -> EventGroup:
    """Create the implicit Event Group for an inbound update."""
    return _ImplicitEventGroup(
        temporalio.api.sdk.v1.EventGroupMarker(
            inbound_update=temporalio.api.sdk.v1.EventGroupMarker.InboundUpdate(
                inbound_update_id=update_id
            )
        )
    )


def _event_group_markers_to_proto(
    event_groups: Sequence[EventGroup] | None,
    payload_converter: temporalio.converter.PayloadConverter,
) -> list[temporalio.api.sdk.v1.EventGroupMarker]:
    """Merge the given Event Groups with those active in the current scope and
    serialize them as the markers attached to a workflow command.

    Must be called from the context the command was requested in, which is not
    necessarily the one it is ultimately built in.
    """
    active = _active_event_groups.get()
    explicit = active.explicit
    for group in event_groups or ():
        if not isinstance(group, _LabelEventGroup):
            raise TypeError(
                "Event groups must be created with workflow.create_event_group()"
            )
        explicit = _with_group(explicit, group)
    groups: list[EventGroup] = list(explicit)
    if active.implicit:
        groups.insert(0, active.implicit)
    return [group._to_proto(payload_converter) for group in groups]


def _with_group(
    groups: tuple[_LabelEventGroup, ...], group: _LabelEventGroup
) -> tuple[_LabelEventGroup, ...]:
    """Add a group to a set of groups, deduplicating by id."""
    if any(existing._id == group._id for existing in groups):
        return tuple(
            group if existing._id == group._id else existing for existing in groups
        )
    return (*groups, group)
