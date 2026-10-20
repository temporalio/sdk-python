"""Helpers for time-skipping tests."""

from __future__ import annotations

import platform

import pytest

from temporalio.api.enums.v1 import event_type_pb2 as _event_type
from temporalio.client import WorkflowHandle


def skip_if_time_skipping_v1_unsupported() -> None:
    """Skip when a V1 time-skipping environment cannot be started here."""
    if platform.machine() not in ("i386", "AMD64", "x86_64"):
        pytest.skip("Time skipping server does not run outside x86")


async def _has_time_skipping_transitioned_event(handle: WorkflowHandle) -> bool:
    """Return True iff the workflow history contains the time-skipping transition event
    ``EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED``."""
    async for e in handle.fetch_history_events():
        if (
            e.event_type
            == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
        ):
            return True
    return False


async def assert_time_was_skipped(handle: WorkflowHandle) -> None:
    """Fail if the workflow history has no time-skipping event."""
    assert await _has_time_skipping_transitioned_event(handle), (
        f"workflow {handle.id} has no TIME_SKIPPING_TRANSITIONED events — "
        f"time skipping never engaged"
    )


async def assert_time_was_not_skipped(handle: WorkflowHandle) -> None:
    """Fail if the workflow history has any time-skipping event."""
    assert not await _has_time_skipping_transitioned_event(handle), (
        f"workflow {handle.id} has at least one TIME_SKIPPING_TRANSITIONED "
        f"event — time skipping engaged unexpectedly"
    )
