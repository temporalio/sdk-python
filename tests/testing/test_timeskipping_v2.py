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
from temporalio.testing import TimeSkipper, TimeSkippingConfig, WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import assert_duration_same, new_worker
from tests.helpers.time_skipping import (
    assert_time_was_not_skipped,
    assert_time_was_skipped,
)
from tests.testing.test_workflow import SleepWorkflow


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
    """Completes after receiving ``required_signals`` ``proceed`` signals; otherwise waits up to 10h."""

    def __init__(self) -> None:
        self.signals_received = 0

    @workflow.run
    async def run(self, required_signals: int) -> str:
        await workflow.wait_condition(
            lambda: self.signals_received >= required_signals,
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
                2,
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


async def test_multi_fast_forward_accumulation(env: WorkflowEnvironment) -> None:
    """Three sequential 1h fast-forwards accumulate to ~3h virtual time.

    Catches off-by-one bugs where each FF's target might be computed from a
    stale reference (e.g. the start time, or the last FF's target) instead
    of the workflow's current virtual clock.
    """
    async with new_worker(env.client, InteractionWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                InteractionWorkflow.run,
                3,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        t0 = await handle.query(InteractionWorkflow.current_time)

        for i, expected in enumerate((3600, 7200, 10800), start=1):
            assert await env.fast_forward(handle, timedelta(hours=1)), (
                f"expected fast-forward {i} to complete"
            )
            await handle.signal(InteractionWorkflow.proceed)
            assert await handle.query(InteractionWorkflow.get_signal_count) == i
            t = await handle.query(InteractionWorkflow.current_time)
            assert_duration_same(expected, t - t0, tolerance=10)

        assert await handle.result() == "done"
        await assert_time_was_skipped(handle)


async def test_partial_fast_forward_then_unbounded(
    env: WorkflowEnvironment,
) -> None:
    """30m fast-forward, verify +30m, then unbounded resume to completion at +1h."""
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        async def wf_now() -> float:
            # Signal first so the query reads workflow.now() as of a fresh
            # non-query workflow task, not the workflow's initial one.
            await handle.signal(SleepWorkflow.tick)
            return await handle.query(SleepWorkflow.current_time)

        t0 = await wf_now()

        # Fast-forward 30m; TS auto-disables at that point.
        assert await env.fast_forward(handle, timedelta(minutes=30))
        t1 = await wf_now()
        assert_duration_same(30 * 60, t1 - t0, tolerance=10)

        # Unbounded resume — the workflow's remaining 30m timer fires and it
        # completes. fast_forward returns False (terminal, no
        # disabled_after_fast_forward event) which we don't need to check.
        await env.fast_forward(handle, None)
        await handle.result()

        # Final virtual time on the closed workflow is ~+1h from start.
        t_end = await handle.query(SleepWorkflow.current_time)
        assert_duration_same(3600, t_end - t0, tolerance=10)

        await assert_time_was_skipped(handle)


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
            SleepWorkflow.run,
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
            "child_start": child_times["start"],
            "child_end": child_times["end"],
        }


async def test_child_workflow_propagates_time_skipping(
    env: WorkflowEnvironment,
) -> None:
    """Parent 1h + child 1h + parent 1h all auto-skip; child inherits TS from parent."""
    async with new_worker(
        env.client, ParentTimeSkippingWorkflow, SleepWorkflow
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
            env.client, ParentTimeSkippingWorkflow, SleepWorkflow
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


async def test_timeskipper_wrapping_local_env_client() -> None:
    """Start a plain local server, wrap its client with a TimeSkipper, run a workflow.

    Exercises the public ``TimeSkipper(client)`` entry point directly:
    no ``ts_config`` on the env, so ``env.client`` has no TS interceptor,
    but the ``TimeSkipper`` instance we build ourselves provides one.
    Workflows started via ``skipper.client`` should get TS stamped and
    the server should skip their idle time.
    """
    async with await WorkflowEnvironment.start_local(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.TimeSkippingEnabled=true",
        ],
    ) as env:
        assert not env.supports_time_skipping

        skipper = TimeSkipper(env.client)
        async with new_worker(skipper.client, SleepWorkflow) as worker:
            wall_start = monotonic()
            handle = await skipper.client.start_workflow(
                SleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()
            wall_elapsed = monotonic() - wall_start

        # Virtual clock advanced ~1h.
        assert_duration_same(3600, result["end"] - result["start"], tolerance=10)
        # But wall time was seconds, not an hour.
        assert wall_elapsed < 10, (
            f"expected fast wall finish under TS, got {wall_elapsed:.1f}s"
        )
        # And the workflow's history has the TS transition event.
        await assert_time_was_skipped(handle)


async def test_fast_forward_returns_false_when_workflow_terminates_first(
    env: WorkflowEnvironment,
) -> None:
    """Workflow's own 1h timer fires before FF's 2h target → FF returns False.

    Exercises the False-return branch of
    ``_wait_for_fast_forward_completed``: the FF's update RPC lands while
    the workflow is still alive, then the workflow's timer fires and
    terminates it before the FF reaches its target, so the wait loop sees
    a terminal event before any ``disabled_after_fast_forward`` transition.
    """
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert (await env.fast_forward(handle, timedelta(hours=2))) is False
