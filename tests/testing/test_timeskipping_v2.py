"""Tests for per-workflow time skipping via the V2 test env."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

import pytest

from temporalio import workflow
from temporalio.api.enums.v1 import event_type_pb2 as _event_type
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import TimeSkipper, TimeSkippingConfig, WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import (
    assert_duration_same,
    assert_eventually,
    find_history_event,
    new_worker,
)
from tests.helpers.time_skipping import (
    assert_time_was_not_skipped,
    assert_time_was_skipped,
)
from tests.testing.test_workflow import SleepWorkflow


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
    virtual_elapsed = result["end"] - result["start"]
    assert virtual_elapsed >= 3600, (
        f"virtual elapsed was {virtual_elapsed}s; expected >= 3600s (timer did not fire fully)"
    )
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
        # explicit fast-forward. Uses signals to move the workflow along.
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                InteractionWorkflow.run,
                2,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        t0 = await env.get_current_time(handle)

        assert await env.fast_forward(handle, timedelta(hours=1)), (
            "expected first fast-forward to complete at 1h"
        )
        await handle.signal(InteractionWorkflow.proceed)
        assert await handle.query(InteractionWorkflow.get_signal_count) == 1
        t1 = await env.get_current_time(handle)
        assert_duration_same(3600, (t1 - t0).total_seconds(), tolerance=10)

        assert await env.fast_forward(handle, timedelta(hours=1)), (
            "expected second fast-forward to complete at 2h total"
        )
        await handle.signal(InteractionWorkflow.proceed)
        t2 = await env.get_current_time(handle)
        assert_duration_same(7200, (t2 - t0).total_seconds(), tolerance=10)

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

        t0 = await env.get_current_time(handle)

        assert await env.fast_forward(handle, timedelta(minutes=30))
        t1 = await env.get_current_time(handle)
        assert_duration_same(30 * 60, (t1 - t0).total_seconds(), tolerance=10)

        assert not await env.fast_forward(handle, None)
        result = await handle.result()
        assert result["message"] == "all done"

        t_end = await env.get_current_time(handle)
        assert_duration_same(3600, (t_end - t0).total_seconds(), tolerance=10)

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

    assert wall_elapsed < 10, (
        f"parent+child took {wall_elapsed:.1f}s wall time; expected < 10s"
    )
    assert_duration_same(
        3600, result["parent_after_wait_1"] - result["parent_start"], tolerance=10
    )
    assert_duration_same(
        3600, result["child_end"] - result["child_start"], tolerance=10
    )
    assert_duration_same(
        3600, result["parent_end"] - result["parent_after_child_start"], tolerance=10
    )
    assert_duration_same(
        0, result["child_start"] - result["parent_after_wait_1"], tolerance=10
    )
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
            "frontend.WorkflowTimeSkippingEnabled=true",
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
        # Child runs in real time; parent's two 1h waits are skipped.
        assert 4 < wall_elapsed < 15, (
            f"expected ~5s wall time (child didn't skip), got {wall_elapsed:.1f}s"
        )

        await assert_time_was_skipped(parent_handle)
        child_handle = env.client.get_workflow_handle(child_id)
        await assert_time_was_not_skipped(child_handle)


async def test_timeskipper_wrapping_local_env_client() -> None:
    """Test timeskipping through direct use of a TimeSkipper, instead of
    indirectly through the time skipping V2 test env.
    """

    async with await WorkflowEnvironment.start_local(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.WorkflowTimeSkippingEnabled=true",
        ],
    ) as env:
        assert not env.supports_time_skipping_v1

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
        assert_duration_same(3600, result["end"] - result["start"], tolerance=10)
        assert wall_elapsed < 10, (
            f"expected fast wall finish under time skipping, got {wall_elapsed:.1f}s"
        )
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


async def test_fast_forward_accepts_float_duration(
    env: WorkflowEnvironment,
) -> None:
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                3600.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        t0 = await env.get_current_time(handle)
        assert await env.fast_forward(handle, 1800.0)
        t1 = await env.get_current_time(handle)
        assert_duration_same(1800, (t1 - t0).total_seconds(), tolerance=10)
        await handle.cancel()
        await assert_time_was_skipped(handle)


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
    """FF spans across retry: attempt 1 fails, backoff elapses, attempt 2 runs."""
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

        # Fast forward into the second sleep.
        assert await env.fast_forward(handle, timedelta(hours=2, minutes=30))

        async def _in_attempt_2() -> None:
            assert (await handle.query(FailOnceThenSleepWorkflow.attempt)) == 2
        await assert_eventually(_in_attempt_2)

        await env.fast_forward(handle)
        assert (await handle.result()) == "done"
        assert (await handle.query(FailOnceThenSleepWorkflow.attempt)) == 2
        await assert_time_was_skipped(handle)


@workflow.defn
class ContinueAsNewSleepWorkflow:
    """Sleep and CAN until ``runs_remaining`` is 1."""

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
    """Fast forward spans multiple continue-as-new runs."""
    async with new_worker(env.client, ContinueAsNewSleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                ContinueAsNewSleepWorkflow.run,
                args=[3600.0, 3, 1],
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert await env.fast_forward(handle, timedelta(hours=2))

        async def _in_run_3() -> None:
            assert (await handle.query(ContinueAsNewSleepWorkflow.current_run)) == 3
        await assert_eventually(_in_run_3)

        await env.fast_forward(handle)
        assert (await handle.result()) == "done"
        assert (await handle.query(ContinueAsNewSleepWorkflow.current_run)) == 3
        await assert_time_was_skipped(handle)


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

            async def _at_least_three_cron_runs() -> None:
                run_count = 0
                async for _ in env.client.list_workflows(
                    query=f"WorkflowId = '{workflow_id}'"
                ):
                    run_count += 1
                assert run_count >= 3, (
                    f"expected >= 3 cron runs after 3h FF, got {run_count}"
                )

            await assert_eventually(_at_least_three_cron_runs)
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
        handle = await env.client.start_workflow(
            SignalWithStartTargetWorkflow.run,
            3600.0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            start_signal="go",
        )
        result = await handle.result()
        wall_elapsed = monotonic() - wall_start

    assert wall_elapsed < 10
    virtual_elapsed = result["end"] - result["after_signal"]
    assert_duration_same(3600, virtual_elapsed, tolerance=50)
    await assert_time_was_skipped(handle)


async def test_get_time_skipping_info_during_workflow(
    env: WorkflowEnvironment,
) -> None:
    async with new_worker(env.client, InteractionWorkflow) as worker:
        handle = await env.client.start_workflow(
            InteractionWorkflow.run,
            1,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        try:
            tsi = await env.get_time_skipping_info(handle)
            assert tsi is not None
            assert tsi.effective_config.enabled, (
                "expected time skipping to be enabled (env-stamped)"
            )
            assert tsi.HasField("current_time"), (
                "TimeSkippingInfo.current_time is not populated"
            )
            assert not tsi.HasField("fast_forward_info"), (
                "no fast-forward was issued; expected fast_forward_info unset"
            )
        finally:
            await handle.signal(InteractionWorkflow.proceed)
            await handle.result()


async def test_get_time_skipping_info_returns_none_when_ts_never_enabled(
    env: WorkflowEnvironment,
) -> None:
    async with new_worker(env.client, InteractionWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                InteractionWorkflow.run,
                1,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        try:
            assert await env.get_time_skipping_info(handle) is None
        finally:
            await handle.signal(InteractionWorkflow.proceed)
            await handle.result()


async def test_max_session_skip_count_stamped_by_env() -> None:
    """Set ``max_session_skip_count`` and confirm it in the
    WorkflowExecutionStarted event."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.WorkflowTimeSkippingEnabled=true",
        ],
        ts_config=TimeSkippingConfig(enabled=True, max_session_skip_count=5),
    ) as env:
        async with new_worker(env.client, InteractionWorkflow) as worker:
            handle = await env.client.start_workflow(
                InteractionWorkflow.run,
                1,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            try:
                started = await find_history_event(
                    handle,
                    lambda e: e.event_type
                    == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
                )
                assert started is not None
                started_tsc = (
                    started.workflow_execution_started_event_attributes.time_skipping_config
                )
                assert started_tsc.max_session_skip_count == 5, (
                    f"expected max_session_skip_count=5, got {started_tsc.max_session_skip_count}"
                )
            finally:
                await handle.signal(InteractionWorkflow.proceed)
                await handle.result()


async def test_transition_event_payload(env: WorkflowEnvironment) -> None:
    """The disabled-after-fast-forward transition event's payload is populated,
    specifically ``target_time`` and ``wall_clock_time``."""
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

        event = await find_history_event(
            handle,
            lambda e: (
                e.event_type
                == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_SKIPPING_TRANSITIONED
                and e.workflow_execution_time_skipping_transitioned_event_attributes.disabled_after_fast_forward
            ),
        )
        assert event is not None, "no disabled_after_fast_forward transition found"
        transition = event.workflow_execution_time_skipping_transitioned_event_attributes

        target = transition.target_time.ToDatetime().replace(tzinfo=timezone.utc)
        target_offset = (target - wall_before_ff).total_seconds()
        assert_duration_same(1800, target_offset, tolerance=20)

        # wall_clock_time should be around wall clock time when fast forward started.
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
        event = await find_history_event(
            child_handle,
            lambda e: e.event_type == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
        )
        assert event is not None
        started = event.workflow_execution_started_event_attributes
        assert started.HasField("time_skipping_state_propagation"), (
            "child's WorkflowExecutionStarted event has no time_skipping_state_propagation"
        )


async def test_fast_forward_exceeds_execution_timeout(
    env: WorkflowEnvironment,
) -> None:
    async with new_worker(env.client, SleepWorkflow) as worker:
        with env.with_time_skipping_disabled():
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                100000.0,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(minutes=30),
            )
        assert (await env.fast_forward(handle, timedelta(hours=1))) is False

        timed_out = await find_history_event(
            handle,
            lambda e: e.event_type
            == _event_type.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
        )
        assert timed_out is not None, "expected WORKFLOW_EXECUTION_TIMED_OUT in history"


async def test_overriding_fast_forward_raises_on_original(
    env: WorkflowEnvironment,
) -> None:
    task_queue = f"tq-{uuid.uuid4()}"
    with env.with_time_skipping_disabled():
        handle = await env.client.start_workflow(
            SleepWorkflow.run,
            100000.0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=task_queue,
        )
    # No worker yet, to keep fast forwards from finishing.

    original = asyncio.create_task(env.fast_forward(handle, timedelta(hours=2)))

    async def _wait_for_ff_id(
        expect_change_from: str | None,
    ) -> str:
        """Poll the workflow's TimeSkippingInfo until fast_forward_info.
        fast_forward_id is set and, if given, differs from the previous id."""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if original.done() and expect_change_from is None:
                val = original.result()  # re-raise if it already errored
                raise AssertionError(
                    f"first completed before second ran (returned {val!r})"
                )
            tsi = await env.get_time_skipping_info(handle)
            ffi = tsi.fast_forward_info if tsi is not None else None
            if ffi and ffi.fast_forward_id and ffi.fast_forward_id != expect_change_from:
                return ffi.fast_forward_id
            await asyncio.sleep(0.05)
        raise AssertionError("timed out waiting for expected fast_forward_id")

    first_ff = await _wait_for_ff_id(expect_change_from=None)

    second_ff = asyncio.create_task(env.fast_forward(handle, timedelta(minutes=30)))
    await _wait_for_ff_id(expect_change_from=first_ff)

    with pytest.raises(RuntimeError, match=r"does not match"):
        await original

    # Attach a worker so the workflow can complete and the second fast-forward can happen.
    async with new_worker(env.client, SleepWorkflow, task_queue=task_queue):
        assert await second_ff
        await env.fast_forward(handle)
        await handle.result()
        await assert_time_was_skipped(handle)
