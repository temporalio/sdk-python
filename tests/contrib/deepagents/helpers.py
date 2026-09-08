"""Shared helpers for the Deep Agents plugin test suite."""

from collections import Counter

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle


async def count_scheduled_activities(handle: WorkflowHandle) -> Counter:
    """Count ``ActivityTaskScheduled`` events by activity-type name."""
    counts: Counter = Counter()
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            counts[name] += 1
    return counts
