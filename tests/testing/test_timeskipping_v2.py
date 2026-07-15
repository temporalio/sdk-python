"""Tests for per-workflow time skipping via the test env API.

Two usage patterns are exercised:

- **Basic**: default env (TS enabled) allows waits to auto-skip until completion.
- **Interactive**: fast-forward by a duration; when that fast-forward
  completes, signal or update the workflow, then resume — either with
  another fast-forward duration, or with no argument to let time skipping
  forward idle time until completion.

In either case, time skipping only happens when there is no in-flight work,
so even when a fast-forward is set, a workflow with no idle time can simply
run to completion.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from time import monotonic

import pytest
import pytest_asyncio

from temporalio import workflow
from temporalio.testing import TimeSkippingConfig, WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import assert_duration_same, new_worker
from tests.helpers.time_skipping import (
    assert_time_was_not_skipped,
    assert_time_was_skipped,
)


@pytest_asyncio.fixture(scope="module")  # type: ignore[reportUntypedFunctionDecorator]
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    """Spawn a module-scoped time-skipping v2 dev server for the tests in this file."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.TimeSkippingEnabled=true",
        ],
    ) as workflow_env:
        yield workflow_env


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

    @workflow.query
    def current_time(self) -> float:
        return workflow.now().timestamp()


async def test_skip_full_run(env: WorkflowEnvironment) -> None:
    """Enable time skipping, let workflow run to completion."""
    async with new_worker(env.client, SingleTimerWorkflow) as worker:
        wall_start = monotonic()
        handle = await env.client.start_workflow(
            SingleTimerWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    # Virtual time advanced by ~1h even though wall time was just a few seconds.
    assert result >= 3600, (
        f"virtual elapsed was {result}s; expected >= 3600s (timer did not fire fully)"
    )
    # 1-hour timer should be auto-skipped in well under 3s of wall time.
    assert wall_elapsed < 3, (
        f"workflow took {wall_elapsed:.3f}s wall time; time skipping did not engage"
    )
    await assert_time_was_skipped(handle)


async def test_with_time_skipping_disabled(
    env: WorkflowEnvironment,
) -> None:
    """Without time skipping, the 1h timer does not complete in 3s."""
    async with new_worker(env.client, SingleTimerWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SingleTimerWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle.result(), timeout=3)


async def test_fast_forward_with_resume(env: WorkflowEnvironment) -> None:
    """Fast-forward 1h, signal, resume +1h, signal, workflow completes."""
    async with new_worker(env.client, InteractionWorkflow) as worker:
        wall_start = monotonic()
        # Start the workflow with TS stamping suspended, then issue an
        # explicit fast-forward. Keeps auto-skip from blowing through the
        # 10h wait_condition timeout before the test can interact.
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                InteractionWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        # Baseline: workflow's virtual clock before any fast-forward.
        t0 = await handle.query(InteractionWorkflow.current_time)

        # Fast-forward 1h; skipping pauses so we can interact.
        assert await env.fast_forward(handle, timedelta(hours=1)), (
            "expected first fast-forward to complete at 1h"
        )
        await handle.signal(InteractionWorkflow.proceed)
        assert await handle.query(InteractionWorkflow.get_signal_count) == 1
        t1 = await handle.query(InteractionWorkflow.current_time)
        assert_duration_same(3600, t1 - t0, tolerance=10)

        # Fast-forward another 1h, then send the second signal to release.
        assert await env.fast_forward(handle, timedelta(hours=1)), (
            "expected second fast-forward to complete at 2h total"
        )
        await handle.signal(InteractionWorkflow.proceed)
        t2 = await handle.query(InteractionWorkflow.current_time)
        assert_duration_same(7200, t2 - t0, tolerance=10)

        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    assert result == "done"
    assert wall_elapsed < 60, (
        f"workflow took {wall_elapsed:.1f}s wall time; expected fast finish"
    )
    await assert_time_was_skipped(handle)


@workflow.defn
class SleepingChildWorkflow:
    """Sleeps a parameterized duration. Returns virtual clock at start and end."""

    @workflow.run
    async def run(self, sleep_seconds: float) -> dict[str, float]:
        t_start = workflow.now().timestamp()
        await workflow.sleep(timedelta(seconds=sleep_seconds))
        t_end = workflow.now().timestamp()
        return {"child_start": t_start, "child_end": t_end}


@workflow.defn
class ParentTimeSkippingWorkflow:
    """1h sleep, spawn child (parameterized sleep), 1h sleep.

    All three waits auto-skip when TS is on for both parent and child. The
    parent's virtual clock only advances during its own waits, though —
    waiting for the child does not skip the parent's clock forward to match
    the child's end time. TS propagates forward at spawn but not backward
    at completion.
    """

    @workflow.run
    async def run(
        self, child_id: str, task_queue: str, child_sleep_seconds: float
    ) -> dict[str, float]:
        parent_start = workflow.now().timestamp()
        await workflow.sleep(timedelta(hours=1))
        parent_after_wait_1 = workflow.now().timestamp()

        child_times = await workflow.execute_child_workflow(
            SleepingChildWorkflow.run,
            child_sleep_seconds,
            id=child_id,
            task_queue=task_queue,
        )
        parent_after_child = workflow.now().timestamp()

        await workflow.sleep(timedelta(hours=1))
        parent_end = workflow.now().timestamp()

        return {
            "parent_start": parent_start,
            "parent_after_wait_1": parent_after_wait_1,
            "parent_after_child": parent_after_child,
            "parent_end": parent_end,
            **child_times,
        }


async def test_child_workflow_propagates_time_skipping(
    env: WorkflowEnvironment,
) -> None:
    """Parent 1h + child 1h + parent 1h all auto-skip; child inherits TS from parent."""
    async with new_worker(
        env.client, ParentTimeSkippingWorkflow, SleepingChildWorkflow
    ) as worker:
        child_id = f"child-{uuid.uuid4()}"
        parent_id = f"parent-{uuid.uuid4()}"

        wall_start = monotonic()
        parent_handle = await env.client.start_workflow(
            ParentTimeSkippingWorkflow.run,
            args=[child_id, worker.task_queue, 3600.0],
            id=parent_id,
            task_queue=worker.task_queue,
        )
        result = await parent_handle.result()
        wall_elapsed = monotonic() - wall_start

    # Total 3h of virtual work should complete in a few seconds of wall time.
    assert wall_elapsed < 10, (
        f"parent+child took {wall_elapsed:.1f}s wall time; expected < 10s"
    )

    # Each 1h wait should have advanced the workflow's clock by ~3600s.
    assert_duration_same(
        3600, result["parent_after_wait_1"] - result["parent_start"], tolerance=10
    )
    assert_duration_same(
        3600, result["child_end"] - result["child_start"], tolerance=10
    )
    assert_duration_same(
        3600, result["parent_end"] - result["parent_after_child"], tolerance=10
    )

    # Forward propagation: child's clock at start matches parent's clock at spawn.
    assert_duration_same(
        0, result["child_start"] - result["parent_after_wait_1"], tolerance=10
    )
    # Parent's clock does not advance while child is running (no backward
    # propagation from child at completion).
    assert_duration_same(
        0, result["parent_after_child"] - result["parent_after_wait_1"], tolerance=5
    )

    # TS engaged on both workflows.
    await assert_time_was_skipped(parent_handle)
    child_handle = env.client.get_workflow_handle(child_id)
    await assert_time_was_skipped(child_handle)


async def test_child_workflow_with_propagation_disabled() -> None:
    """With ``disable_propagation=True`` on the env, child does NOT inherit TS
    and runs in real time."""
    
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.TimeSkippingEnabled=true",
        ],
        ts_config=TimeSkippingConfig(disable_propagation=True),
    ) as env:
        async with new_worker(
            env.client, ParentTimeSkippingWorkflow, SleepingChildWorkflow
        ) as worker:
            child_id = f"child-{uuid.uuid4()}"
            parent_id = f"parent-{uuid.uuid4()}"

            wall_start = monotonic()
            parent_handle = await env.client.start_workflow(
                ParentTimeSkippingWorkflow.run,
                args=[child_id, worker.task_queue, 5.0],
                id=parent_id,
                task_queue=worker.task_queue,
            )
            await parent_handle.result()
            wall_elapsed = monotonic() - wall_start

        # Child's 5s sleep runs in real time; parent's two 1h waits skip.
        # Total wall time is dominated by the child's real sleep.
        assert 4 < wall_elapsed < 15, (
            f"expected ~5s wall time (child didn't skip), got {wall_elapsed:.1f}s"
        )

        # Parent had TS engaged (its own waits skipped); child did not.
        await assert_time_was_skipped(parent_handle)
        child_handle = env.client.get_workflow_handle(child_id)
        await assert_time_was_not_skipped(child_handle)
