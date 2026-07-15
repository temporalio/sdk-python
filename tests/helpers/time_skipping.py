"""Test-side assertions that per-workflow time skipping actually engaged."""

from __future__ import annotations

from temporalio.api.enums.v1 import event_type_pb2 as _event_type
from temporalio.client import WorkflowHandle


async def assert_time_skipping_engaged(handle: WorkflowHandle) -> None:
    """Fail if the workflow history has no TIME_SKIPPING_TRANSITIONED event.

    That event is the only one the server emits exclusively under
    per-workflow time skipping — its presence is proof TS actually ran
    for this workflow. Tests that exercise TS should call this at the
    end; tests that specifically exercise TS being OFF should not.
    """
    transitions = [
        e
        async for e in handle.fetch_history_events()
        if e.event_type
        == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
    ]
    assert transitions, (
        f"workflow {handle.id} has no TIME_SKIPPING_TRANSITIONED events — "
        f"time skipping never engaged"
    )
