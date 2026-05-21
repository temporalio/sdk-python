"""Tests for per-workflow time skipping.

Two usage patterns are exercised:

- **Basic**: enable time skipping on a client to forward user timers until completion.
- **Interactive (complicated)**: forward the time by a duration before completion and when it is done,
  notify the test to signal or update, and then resume skipping with another duration to forward or
  no duration to let the time skipping only forward user timers until completion.
in either case, time skipping only happens when there is no in-flight work, so even if a duration is set
to forward but the workflow can just run to completion (for example a workflow that has no idle time).

The ``env`` fixture from ``conftest.py`` works unchanged for cloud — no
separate cloud-only env fixture is needed.
"""

import asyncio
import uuid
from datetime import timedelta
from time import monotonic

import pytest

from temporalio import workflow
from temporalio.testing import (
    WorkflowEnvironment,
    WorkflowTimeSkipper,
    WorkflowTimeSkippingConfig,
)
from tests.helpers import new_worker


@workflow.defn
class SingleTimerWorkflow:
    @workflow.run
    async def run(self) -> float:
        """Sleep 1h of virtual time and return the elapsed virtual seconds."""
        start = workflow.now()
        await workflow.sleep(timedelta(hours=1))
        return (workflow.now() - start).total_seconds()


@workflow.defn
class InteractionWorkflow:
    """Completes after receiving two ``proceed`` signals; otherwise waits up to 10h."""

    def __init__(self) -> None:
        self.signals_received = 0

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(
            lambda: self.signals_received >= 2,
            timeout=timedelta(hours=10),
        )
        return "done"

    @workflow.signal
    def proceed(self) -> None:
        self.signals_received += 1

    @workflow.query
    def get_signal_count(self) -> int:
        return self.signals_received


async def test_pattern_basic(env: WorkflowEnvironment) -> None:
    """Pattern 1: enable time skipping, let workflow run to completion."""
    ts = WorkflowTimeSkipper(env.client)
    async with new_worker(ts.client, SingleTimerWorkflow) as worker:
        wall_start = monotonic()
        result = await ts.client.execute_workflow(
            SingleTimerWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        wall_elapsed = monotonic() - wall_start

    # Virtual time advanced by ~1h even though wall time was just a few seconds.
    assert result >= 3600, (
        f"virtual elapsed was {result}s; expected >= 3600s (timer did not fire fully)"
    )
    # 1-hour timer should be auto-skipped in well under 3s of wall time.
    assert wall_elapsed < 3, (
        f"workflow took {wall_elapsed:.3f}s wall time; time skipping did not engage"
    )


async def test_pattern_basic_no_skipping_times_out(
    env: WorkflowEnvironment,
) -> None:
    """Without time skipping, the 1h timer does not complete in 3s."""
    async with new_worker(env.client, SingleTimerWorkflow) as worker:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                env.client.execute_workflow(
                    SingleTimerWorkflow.run,
                    id=f"wf-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                ),
                timeout=3,
            )


async def test_pattern2_bounded_with_resume(env: WorkflowEnvironment) -> None:
    """Pattern 2: skip 1h, signal, resume +1h, signal, workflow completes.

    The workflow would otherwise sit on a 10-hour timer waiting for two
    signals. Time skipping advances virtual time to each interaction point;
    the test sends a signal once skipping pauses at each bound.

    TODO: this requires a dev-server build that enforces
    ``TimeSkippingConfig.max_skipped_duration`` and
    emits ``WorkflowExecutionTimeSkippingTransitioned`` events. The currently
    downloaded CLI does not — point ``conftest.env`` at a local build of the
    CLI branch that ships the bound + transition-event API once it lands.
    """
    ts = WorkflowTimeSkipper(
        env.client,
        config=WorkflowTimeSkippingConfig(max_skip_duration=timedelta(hours=1)),
    )
    async with new_worker(ts.client, InteractionWorkflow) as worker:
        wall_start = monotonic()
        handle = await ts.client.start_workflow(
            InteractionWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Skip 1h of virtual time; bound pauses skipping so we can interact.
        assert await ts.wait_for_skip_duration_reached(handle), (
            "expected first bound at 1h"
        )
        await handle.signal(InteractionWorkflow.proceed)
        assert await handle.query(InteractionWorkflow.get_signal_count) == 1

        # Skip another 1h, then send the second signal to release the workflow.
        await ts.resume(handle, delta=timedelta(hours=1))
        assert await ts.wait_for_skip_duration_reached(handle), (
            "expected second bound at 2h total"
        )
        await handle.signal(InteractionWorkflow.proceed)

        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    assert result == "done"
    assert wall_elapsed < 60, (
        f"workflow took {wall_elapsed:.1f}s wall time; expected fast finish"
    )
