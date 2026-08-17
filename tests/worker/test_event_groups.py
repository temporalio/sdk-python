"""Tests for Event Groups."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.sdk.v1 import EventGroupMarker
from temporalio.client import Client, WorkflowHandle
from temporalio.converter import PayloadConverter
from temporalio.exceptions import TemporalError
from tests.helpers import new_worker


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


def _label_ids(event: HistoryEvent) -> list[str]:
    return [m.label.id for m in event.event_group_markers if m.HasField("label")]


def _labels(event: HistoryEvent) -> list[str]:
    return [
        PayloadConverter.default.from_payload(m.label.label)
        for m in event.event_group_markers
        if m.HasField("label")
    ]


def _implicit_markers(event: HistoryEvent) -> list[EventGroupMarker]:
    return [m for m in event.event_group_markers if not m.HasField("label")]


async def _events_by_type(handle: WorkflowHandle) -> Mapping[int, list[HistoryEvent]]:
    events: dict[int, list[HistoryEvent]] = {}
    async for event in handle.fetch_history_events():
        events.setdefault(event.event_type, []).append(event)
    return events


def _expected_id(original_execution_run_id: str, label: str) -> str:
    return hashlib.sha1(f"{original_execution_run_id}{label}".encode()).hexdigest()


@workflow.defn
class EventGroupsWorkflow:
    @workflow.run
    async def run(self) -> None:
        payment_group = workflow.create_event_group("payment-processing")
        customer_group = workflow.create_event_group(
            "customer-james-watkins", id="customer-123456"
        )

        # Explicit attachment
        await workflow.execute_activity(
            say_hello,
            "explicit",
            start_to_close_timeout=timedelta(minutes=1),
            event_groups=[payment_group, customer_group],
        )

        # Scope-based propagation, including a nested scope and a group that is
        # both in scope and explicitly attached
        with payment_group.scope():
            await workflow.execute_activity(
                say_hello,
                "scoped",
                start_to_close_timeout=timedelta(minutes=1),
            )
            with customer_group.scope():
                await workflow.sleep(0.01)
            await workflow.execute_activity(
                say_hello,
                "scoped-and-explicit",
                start_to_close_timeout=timedelta(minutes=1),
                event_groups=[payment_group],
            )

        # Back out of all scopes
        await workflow.execute_activity(
            say_hello,
            "unscoped",
            start_to_close_timeout=timedelta(minutes=1),
        )


async def test_event_groups_attached_to_commands(client: Client):
    async with new_worker(
        client, EventGroupsWorkflow, activities=[say_hello]
    ) as worker:
        handle = await client.start_workflow(
            EventGroupsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()

        events = await _events_by_type(handle)
        scheduled = events[EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED]
        assert len(scheduled) == 4
        explicit, scoped, scoped_and_explicit, unscoped = scheduled

        payment_id = _expected_id(handle.result_run_id or "", "payment-processing")
        assert _label_ids(explicit) == [payment_id, "customer-123456"]
        assert _labels(explicit) == ["payment-processing", "customer-james-watkins"]
        assert _label_ids(scoped) == [payment_id]
        # A group both in scope and explicitly attached is attached once
        assert _label_ids(scoped_and_explicit) == [payment_id]
        assert _label_ids(unscoped) == []

        # Nested scopes accumulate
        timer = events[EventType.EVENT_TYPE_TIMER_STARTED][0]
        assert _label_ids(timer) == [payment_id, "customer-123456"]


@workflow.defn
class ImplicitEventGroupsWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            say_hello, "main", start_to_close_timeout=timedelta(minutes=1)
        )
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal
    async def signal_activity(self) -> None:
        await workflow.execute_activity(
            say_hello, "signal", start_to_close_timeout=timedelta(minutes=1)
        )

    @workflow.update
    async def update_activity(self) -> None:
        await workflow.execute_activity(
            say_hello, "update", start_to_close_timeout=timedelta(minutes=1)
        )

    @workflow.signal
    def done(self) -> None:
        self._done = True


async def test_implicit_event_groups_around_handlers(client: Client):
    async with new_worker(
        client, ImplicitEventGroupsWorkflow, activities=[say_hello]
    ) as worker:
        handle = await client.start_workflow(
            ImplicitEventGroupsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(ImplicitEventGroupsWorkflow.signal_activity)
        await handle.execute_update(ImplicitEventGroupsWorkflow.update_activity)
        await handle.signal(ImplicitEventGroupsWorkflow.done)
        await handle.result()

        events = await _events_by_type(handle)
        started = events[EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED][0]
        signaled = events[EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED][0]
        scheduled = {
            PayloadConverter.default.from_payloads(
                e.activity_task_scheduled_event_attributes.input.payloads
            )[0]: e
            for e in events[EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED]
        }

        assert [
            m.inbound_event.inbound_event_id
            for m in _implicit_markers(scheduled["main"])
        ] == [started.event_id]
        assert [
            m.inbound_event.inbound_event_id
            for m in _implicit_markers(scheduled["signal"])
        ] == [signaled.event_id]
        assert [
            m.inbound_update.inbound_update_id
            for m in _implicit_markers(scheduled["update"])
        ] != [""]


@workflow.defn
class EventGroupIdWorkflow:
    @workflow.run
    async def run(self) -> tuple[str, str, str, str]:
        first = workflow.create_event_group("same-label")
        second = workflow.create_event_group("same-label")
        other = workflow.create_event_group("other-label")
        explicit = workflow.create_event_group("some-label", id="explicit-id")
        return (first._id, second._id, other._id, explicit._id)  # type: ignore[attr-defined]


async def test_event_group_ids_are_derived_from_label(client: Client):
    async with new_worker(client, EventGroupIdWorkflow) as worker:
        handle = await client.start_workflow(
            EventGroupIdWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        first, second, other, explicit = await handle.result()
        assert first == second
        assert first != other
        assert first == _expected_id(handle.result_run_id or "", "same-label")
        assert explicit == "explicit-id"


@workflow.defn
class BadEventGroupWorkflow:
    @workflow.run
    async def run(self, args: Sequence[str]) -> str:
        label, id = args
        try:
            workflow.create_event_group(label, id=id or None)
        except ValueError as err:
            return str(err)
        return "no error"


@pytest.mark.parametrize(
    ("label", "id", "expected"),
    [
        ("", "", "Event group label cannot be empty"),
        ("some-label", "", "no error"),
    ],
)
async def test_event_group_validates_label(
    client: Client, label: str, id: str, expected: str
):
    async with new_worker(client, BadEventGroupWorkflow) as worker:
        assert expected == await client.execute_workflow(
            BadEventGroupWorkflow.run,
            [label, id],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


def test_event_group_requires_workflow_context():
    with pytest.raises(TemporalError):
        workflow.create_event_group("outside-workflow")
