"""Event Groups, a way to group logically related workflow events.

.. warning::
    Event Groups is an experimental API and may change without notice.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import temporalio.api.sdk.v1
import temporalio.converter

from ._context import _Runtime
from ._sandbox import logger

__all__ = [
    "EventGroup",
    "create_event_group",
    "_inbound_event_group",
    "_inbound_update_event_group",
    "_capture_event_group_markers",
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
    def _to_proto(self) -> temporalio.api.sdk.v1.EventGroupMarker:
        """Serialize as the marker attached to a workflow command."""
        ...


class _LabelEventGroup(EventGroup):
    """An Event Group explicitly created by workflow code."""

    def __init__(self, id: str, label: str | None) -> None:
        self._id = id
        self._label = label

    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        return _ActiveEventGroups(
            implicit=active.implicit,
            explicit={**active.explicit, self._id: self},
        )

    def _to_proto(self) -> temporalio.api.sdk.v1.EventGroupMarker:
        if self._label is None:
            return temporalio.api.sdk.v1.EventGroupMarker(
                label=temporalio.api.sdk.v1.EventGroupMarker.Label(id=self._id)
            )
        # Deliberately the SDK's default converter, not the user-provided one.
        return temporalio.api.sdk.v1.EventGroupMarker(
            label=temporalio.api.sdk.v1.EventGroupMarker.Label(
                id=self._id,
                label=temporalio.converter.PayloadConverter.default.to_payload(
                    self._label
                ),
            )
        )


class _ImplicitEventGroup(EventGroup):
    """An Event Group created by the SDK around an inbound signal or update.

    The workflow's main function deliberately gets no such group, so commands it
    produces outside any explicit scope carry no markers at all.
    """

    def __init__(self, marker: temporalio.api.sdk.v1.EventGroupMarker) -> None:
        self._marker = marker

    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        # Implicit groups intentionally do not inherit the enclosing scope: a
        # handler registered inside an explicit scope must not attribute its
        # commands to that scope.
        return _ActiveEventGroups(implicit=self)

    def _to_proto(self) -> temporalio.api.sdk.v1.EventGroupMarker:
        return self._marker


class _StubImplicitEventGroup(EventGroup):
    """No-op implicit group used when an inbound event ID is missing or invalid.

    Event Groups must not fail a workflow task, so the handler still enters a
    scope that isolates it from enclosing explicit groups without emitting a
    marker.
    """

    def _applied_over(self, active: _ActiveEventGroups) -> _ActiveEventGroups:
        return _ActiveEventGroups()

    def _to_proto(self) -> temporalio.api.sdk.v1.EventGroupMarker:
        return temporalio.api.sdk.v1.EventGroupMarker()


@dataclass(frozen=True)
class _ActiveEventGroups:
    implicit: EventGroup | None = None
    explicit: dict[str, _LabelEventGroup] = field(default_factory=dict)


_active_event_groups: contextvars.ContextVar[_ActiveEventGroups] = (
    contextvars.ContextVar(
        "__temporal_active_event_groups", default=_ActiveEventGroups()
    )
)


def create_event_group(id: str, *, label: str | None = None) -> EventGroup:
    """Create an Event Group that can be attached to commands scheduled by this
    workflow.

    Attach the returned group via command ``event_groups`` options, or via
    :py:meth:`EventGroup.scope`.

    Args:
        id: Non-empty group identity. Commands with the same ``id`` belong to
            the same group. The user-provided ID is stored as plain text in the
            workflow history and should therefore not contain sensitive
            information.
        label: Optional non-empty display text for the UI / CLI. If provided,
            it is persisted to history as a codec-encoded Payload.

    .. warning::
        Event Groups is an experimental API and may change without notice.
    """
    _Runtime.current()
    if not id:
        raise ValueError("Event group id cannot be empty")
    if label is not None and not label:
        raise ValueError("Event group label cannot be empty")
    return _LabelEventGroup(id, label)


def _inbound_event_group(event_id: int) -> EventGroup:
    """Create the implicit Event Group for an inbound signal's history event."""
    if event_id <= 0:
        logger.warning(
            "Cannot create implicit Event Group for signal with invalid "
            "originating event ID: %s",
            event_id,
        )
        return _StubImplicitEventGroup()
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


def _capture_event_group_markers(
    directs: Sequence[EventGroup] | None,
) -> list[temporalio.api.sdk.v1.EventGroupMarker]:
    """Snapshot ambient and directly attached Event Groups as command markers.

    Must be called from the context the command was requested in, which is not
    necessarily the one it is ultimately built in.
    """
    active = _active_event_groups.get()
    explicit = active.explicit
    if directs:
        explicit = dict(explicit)
        for group in directs:
            if not isinstance(group, _LabelEventGroup):
                raise TypeError(
                    "Event groups must be created with workflow.create_event_group()"
                )
            explicit[group._id] = group
    groups: list[EventGroup] = list(explicit.values())
    if active.implicit:
        groups.insert(0, active.implicit)
    return [group._to_proto() for group in groups]
