"""Tests for per-workflow time skipping via the v2 test env."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

import pytest
import pytest_asyncio

from temporalio import workflow
from temporalio.api.enums.v1 import event_type_pb2 as _event_type
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
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
    async with new_worker(env.client, SleepWorkflow) as worker:
        wall_start = monotonic()
        handle = await env.client.start_workflow(
            SleepWorkflow.run,
            3600.0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    assert result["message"] == "all done"
    # Virtual time advanced by ~1h even though wall time was just a few seconds.
    virtual_elapsed = result["end"] - result["start"]
    assert virtual_elapsed >= 3600, (
        f"virtual elapsed was {virtual_elapsed}s; expected >= 3600s (timer did not fire fully)"
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
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(handle.result(), timeout=3)


async def test_fast_forward_with_resume(env: WorkflowEnvironment) -> None:
    """Fast-forward 1h, signal, resume +1h, signal, workflow completes."""
    async with new_worker(env.client, InteractionWorkflow) as worker:
        wall_start = monotonic()
        # Start the workflow with time-skipping stamping suspended, then issue an
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
            return await handle.query(SleepWorkflow.now)

        t0 = await wf_now()

        # Fast-forward 30m; time skipping auto-disables at that point.
        assert await env.fast_forward(handle, timedelta(minutes=30))
        t1 = await wf_now()
        assert_duration_same(30 * 60, t1 - t0, tolerance=10)

        # Unbounded resume — the workflow's remaining 30m timer fires and it
        # completes. fast_forward returns False because unbounded (duration=None)
        # has no target, so no disabled_after_fast_forward transition can fire;
        # the wait loop sees only the workflow's terminal event.
        assert not await env.fast_forward(handle, None)
        result = await handle.result()
        assert result["message"] == "all done"

        # Final virtual time on the closed workflow is ~+1h from start.
        t_end = await handle.query(SleepWorkflow.now)
        assert_duration_same(3600, t_end - t0, tolerance=10)

        await assert_time_was_skipped(handle)


@workflow.defn
class ParentTimeSkippingWorkflow:
    """Parent 1h + child 1h + parent 1h all auto-skip; child inherits time skipping from parent."""

    @workflow.run
    async def run(
        self, child_id: str, task_queue: str, child_sleep_seconds: float
    ) -> dict[str, Any]:
        parent_start = workflow.now().timestamp()
        await workflow.sleep(timedelta(hours=1))
        parent_after_wait_1 = workflow.now().timestamp()

        child_result = await workflow.execute_child_workflow(
            SleepWorkflow.run,
            child_sleep_seconds,
            id=child_id,
            task_queue=task_queue,
        )
        parent_after_child_start = workflow.now().timestamp()

        await workflow.sleep(timedelta(hours=1))
        parent_end = workflow.now().timestamp()

        return {
            "parent_start": parent_start,
            "parent_after_wait_1": parent_after_wait_1,
            "parent_after_child_start": parent_after_child_start,
            "parent_end": parent_end,
            "child_start": child_result["start"],
            "child_end": child_result["end"],
            "child_message": child_result["message"],
            "message": "all done",
        }


async def test_child_workflow_propagates_time_skipping(
    env: WorkflowEnvironment,
) -> None:
    """Parent 1h + child 1h + parent 1h all auto-skip; child inherits time skipping from parent."""
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

    assert result["message"] == "all done"
    assert result["child_message"] == "all done"
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
        3600, result["parent_end"] - result["parent_after_child_start"], tolerance=10
    )

    # Forward propagation: child's clock at start matches parent's clock at spawn.
    assert_duration_same(
        0, result["child_start"] - result["parent_after_wait_1"], tolerance=10
    )
    # Parent's clock does not advance while child is running (no backward
    # propagation from child at completion).
    assert_duration_same(
        0, result["parent_after_child_start"] - result["parent_after_wait_1"], tolerance=5
    )

    # Time skipping engaged on both workflows.
    await assert_time_was_skipped(parent_handle)
    child_handle = env.client.get_workflow_handle(child_id)
    await assert_time_was_skipped(child_handle)


async def test_child_workflow_with_propagation_disabled() -> None:
    """With ``disable_propagation=True`` on the env, child does NOT inherit time skipping
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
            result = await parent_handle.result()
            wall_elapsed = monotonic() - wall_start

        assert result["message"] == "all done"
        assert result["child_message"] == "all done"
        # Child's 5s sleep runs in real time; parent's two 1h waits skip.
        # Total wall time is dominated by the child's real sleep.
        assert 4 < wall_elapsed < 15, (
            f"expected ~5s wall time (child didn't skip), got {wall_elapsed:.1f}s"
        )

        # Parent had time skipping engaged (its own waits skipped); child did not.
        await assert_time_was_skipped(parent_handle)
        child_handle = env.client.get_workflow_handle(child_id)
        await assert_time_was_not_skipped(child_handle)


async def test_timeskipper_wrapping_local_env_client() -> None:
    """Test timeskipping through direct use of a TimeSkipper, instead of
    indirectly through the time skipping v2 test env.
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

        assert result["message"] == "all done"
        # Virtual clock advanced ~1h.
        assert_duration_same(3600, result["end"] - result["start"], tolerance=10)
        # But wall time was seconds, not an hour.
        assert wall_elapsed < 10, (
            f"expected fast wall finish under time skipping, got {wall_elapsed:.1f}s"
        )
        # And the workflow's history has the time-skipping transition event.
        await assert_time_was_skipped(handle)


async def test_fast_forward_returns_false_when_workflow_terminates_first(
    env: WorkflowEnvironment,
) -> None:
    """Workflow's own 1h timer fires before FF's 2h target → FF returns False.
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


@workflow.defn
class FailOnceThenSleepWorkflow:
    """Sleeps ``sleep_seconds``; fails on the first attempt, succeeds on later ones."""

    @workflow.run
    async def run(self, sleep_seconds: float) -> str:
        await workflow.sleep(timedelta(seconds=sleep_seconds))
        if workflow.info().attempt < 2:
            raise ApplicationError("first attempt fails on purpose")
        return "done"

    @workflow.query
    def attempt(self) -> int:
        return workflow.info().attempt


async def test_fast_forward_spans_retries(env: WorkflowEnvironment) -> None:
    """FF should cover retry_backoff + attempt 2. Currently fails: FF returns
    False when it sees attempt 1's WORKFLOW_EXECUTION_FAILED event."""
    async with new_worker(env.client, FailOnceThenSleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                FailOnceThenSleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(hours=1),
                    backoff_coefficient=1.0,
                    maximum_attempts=2,
                ),
            )
        # 1h sleep, 1h retry backoff, 1h sleep. 2.5h fast forward should
        # end solidly in the second run.
        assert await env.fast_forward(handle, timedelta(hours=2, minutes=30))
        assert (await handle.result()) == "done"
        assert (await handle.query(FailOnceThenSleepWorkflow.attempt)) == 2


@workflow.defn
class ContinueAsNewSleepWorkflow:
    """Sleeps ``sleep_seconds``, then continues-as-new until ``runs_remaining`` is 1.

    ``current_run`` counts up through the CAN chain (1, 2, 3, ...) and is
    queryable so tests can verify how far the chain progressed.
    """

    def __init__(self) -> None:
        self._current_run = 1

    @workflow.run
    async def run(
        self, sleep_seconds: float, runs_remaining: int, current_run: int = 1
    ) -> str:
        self._current_run = current_run
        await workflow.sleep(timedelta(seconds=sleep_seconds))
        if runs_remaining > 1:
            workflow.continue_as_new(
                args=[sleep_seconds, runs_remaining - 1, current_run + 1]
            )
        return "done"

    @workflow.query
    def current_run(self) -> int:
        return self._current_run


async def test_fast_forward_spans_continue_as_new(env: WorkflowEnvironment) -> None:
    """"Fast forward over 3 continue-as-new runs."""
    async with new_worker(env.client, ContinueAsNewSleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                ContinueAsNewSleepWorkflow.run,
                args=[3600.0, 3, 1],
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert await env.fast_forward(handle, timedelta(hours=2))
        assert (await handle.result()) == "done"
        assert (await handle.query(ContinueAsNewSleepWorkflow.current_run)) == 3


async def test_fast_forward_spans_cron_restarts(
    env: WorkflowEnvironment,
) -> None:
    """Fast forward over multiple cron firings."""
    workflow_id = f"wf-{uuid.uuid4()}"
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                60.0,
                id=workflow_id,
                task_queue=worker.task_queue,
                cron_schedule="@every 1h",
            )
        try:
            assert await env.fast_forward(handle, timedelta(hours=3))
            run_count = 0
            async for _ in env.client.list_workflows(
                query=f"WorkflowId = '{workflow_id}'"
            ):
                run_count += 1
            assert run_count >= 3, (
                f"expected >= 3 cron runs after 3h FF, got {run_count}"
            )
        finally:
            await handle.cancel()


@workflow.defn
class SignalWithStartTargetWorkflow:
    """Waits for at least one ``go`` signal, then does a long sleep."""

    def __init__(self) -> None:
        self._got_signal = False

    @workflow.run
    async def run(self, sleep_seconds: float) -> dict[str, float]:
        await workflow.wait_condition(lambda: self._got_signal)
        t_after_signal = workflow.now().timestamp()
        await workflow.sleep(timedelta(seconds=sleep_seconds))
        t_end = workflow.now().timestamp()
        return {"after_signal": t_after_signal, "end": t_end}

    @workflow.signal
    def go(self) -> None:
        self._got_signal = True


async def test_signal_with_start_stamps_time_skipping_config(
    env: WorkflowEnvironment,
) -> None:
    """Timeskip a signal-with-start workflow."""
    async with new_worker(env.client, SignalWithStartTargetWorkflow) as worker:
        wall_start = monotonic()
        # signal_with_start via start_workflow's start_signal= kwarg.
        handle = await env.client.start_workflow(
            SignalWithStartTargetWorkflow.run,
            3600.0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            start_signal="go",
        )
        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    # 1h sleep was time-skipped → wall time small; virtual clock advanced by ~1h.
    assert wall_elapsed < 10
    virtual_elapsed = result["end"] - result["after_signal"]
    assert 3550 <= virtual_elapsed <= 3650, (
        f"expected ~3600s virtual for the 1h sleep, got {virtual_elapsed}s"
    )
    await assert_time_was_skipped(handle)


async def test_update_time_reflects_workflow_virtual_clock(
    env: WorkflowEnvironment,
) -> None:
    """``UpdateWorkflowExecutionOptionsResponse.update_time`` is the workflow's virtual clock.

    Proto: "The Workflow Execution time when the options were updated. When
    time skipping is enabled, this is the workflow's virtual time rather
    than wall-clock time." Sequence: FF 1h → workflow's virtual clock now
    ~+1h → send another update (set_disable_propagation) → its response's
    update_time should be ~+1h, not ~real-now.
    """
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                100000.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        # FF 1h. Virtual clock advances to +1h and time skipping auto-disables.
        wf_start_wall = datetime.now(tz=timezone.utc)
        assert await env.fast_forward(handle, timedelta(hours=1))
        # Send a follow-up update. The response's update_time reflects the
        # workflow's virtual clock at the moment the update landed, which
        # should be ~+1h from workflow start, not ~real-now.
        resp = await env._ts_skipper._update_time_skipping_config(  # type: ignore[reportPrivateUsage]
            handle, TimeSkippingConfig(enabled=True, disable_propagation=True)
        )
        assert resp.HasField("update_time"), (
            "UpdateWorkflowExecutionOptionsResponse.update_time is not populated"
        )
        update_time = resp.update_time.ToDatetime().replace(tzinfo=timezone.utc)
        # Update time should be at least ~+1h from workflow start wall clock
        # (virtual clock was advanced 1h before this update).
        offset_seconds = (update_time - wf_start_wall).total_seconds()
        assert 3550 <= offset_seconds <= 3700, (
            f"update_time is {offset_seconds}s past wf_start_wall; expected "
            f"~3600s (virtual clock advanced 1h before the update)"
        )
        await handle.cancel()


async def test_transition_event_payload(env: WorkflowEnvironment) -> None:
    """The disabled-after-fast-forward transition event's payload is populated,
    specifically `target_time` and `wall_clock_time`."""
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                100000.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        wall_before_ff = datetime.now(tz=timezone.utc)
        assert await env.fast_forward(handle, timedelta(minutes=30))
        wall_after_ff = datetime.now(tz=timezone.utc)

        # Find the disabled_after_fast_forward transition event in history.
        transition = None
        async for event in handle.fetch_history_events():
            if (
                event.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
            ):
                attrs = event.workflow_execution_time_skipping_transitioned_event_attributes
                if attrs.disabled_after_fast_forward:
                    transition = attrs
                    break
        assert transition is not None, "no disabled_after_fast_forward transition found"

        # target_time: virtual time the FF advanced to. Should be ~+30m from
        # the pre-FF wall clock (since the workflow started with time skipping off,
        # virtual clock at start ≈ wall clock).
        target = transition.target_time.ToDatetime().replace(tzinfo=timezone.utc)
        target_offset = (target - wall_before_ff).total_seconds()
        assert 1780 <= target_offset <= 1820, (
            f"target_time offset {target_offset}s from pre-FF wall; expected ~1800s"
        )

        # wall_clock_time: real time when transition fired. Should be near
        # the wall time we observed around the FF call.
        wct = transition.wall_clock_time.ToDatetime().replace(tzinfo=timezone.utc)
        assert wall_before_ff <= wct <= wall_after_ff + timedelta(seconds=5), (
            f"wall_clock_time {wct} not in expected wall-time window "
            f"[{wall_before_ff}, {wall_after_ff}]"
        )

        await handle.cancel()


async def test_child_workflow_started_event_has_state_propagation(
    env: WorkflowEnvironment,
) -> None:
    """A child workflow spawned under time skipping carries TimeSkippingStatePropagation. """
    async with new_worker(
        env.client, ParentTimeSkippingWorkflow, SleepWorkflow
    ) as worker:
        child_id = f"child-{uuid.uuid4()}"
        parent_id = f"parent-{uuid.uuid4()}"
        parent_handle = await env.client.start_workflow(
            ParentTimeSkippingWorkflow.run,
            args=[child_id, worker.task_queue, 60.0],
            id=parent_id,
            task_queue=worker.task_queue,
        )
        await parent_handle.result()

        child_handle = env.client.get_workflow_handle(child_id)
        started = None
        async for event in child_handle.fetch_history_events():
            if event.event_type == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                started = event.workflow_execution_started_event_attributes
                break
        assert started is not None
        assert started.HasField("time_skipping_state_propagation"), (
            "child's WorkflowExecutionStarted event has no time_skipping_state_propagation"
        )


async def test_fast_forward_clamped_to_execution_timeout(
    env: WorkflowEnvironment,
) -> None:
    """FF duration exceeds execution timeout → workflow times out; FF returns False."""
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                100000.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(minutes=30),
            )
        # FF for 1h — but the workflow's execution_timeout is 30m, so the
        # workflow terminates (TIMED_OUT) at ~30m virtual before the FF
        # target at 1h is reached.
        assert (await env.fast_forward(handle, timedelta(hours=1))) is False

        # Confirm the workflow ended via TIMED_OUT rather than completing.
        timed_out = False
        async for event in handle.fetch_history_events():
            if (
                event.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT
            ):
                timed_out = True
                break
        assert timed_out, "expected WORKFLOW_EXECUTION_TIMED_OUT in history"
