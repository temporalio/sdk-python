"""Tests for WorkflowTimeSkipper (per-workflow time skipping).

These tests require a dev server built with time-skipping support
(cli/temporal.exe) and use --dynamic-config-value frontend.TimeSkippingEnabled=true.
"""

import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ActivityError, TimeoutError, TimeoutType
from temporalio.testing import (
    WorkflowEnvironment,
    WorkflowTimeSkipper,
)
from tests.helpers import assert_eventually, new_worker


@workflow.defn
class SingleTimerWorkflow:
    @workflow.run
    async def run(self) -> str:
        await workflow.sleep(timedelta(hours=1))
        return "done"


async def test_auto_skip_single_timer(env: WorkflowEnvironment):
    ts = WorkflowTimeSkipper(env.client, auto_skip=True)
    async with new_worker(ts.client, SingleTimerWorkflow) as worker:
        handle = await ts.client.start_workflow(
            SingleTimerWorkflow.run,
            id=f"test-auto-skip-single-timer-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Auto-skip fires the 1h timer automatically — workflow completes
        assert await handle.result() == "done"

        # Describe final state
        desc = await ts.describe(handle)
        assert desc.status == WorkflowExecutionStatus.COMPLETED
        assert desc.time_skipping is not None
        assert desc.time_skipping.config is not None
        assert desc.time_skipping.config.enabled is True
        assert desc.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(minutes=59)
            < desc.time_skipping.virtual_time_offset
            < timedelta(hours=1, seconds=10)
        )

        # History: TIME_POINT_ADVANCED before TIMER_FIRED
        history = await handle.fetch_history()
        tp_advanced = [
            e
            for e in history.events
            if e.event_type
            == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_POINT_ADVANCED
        ]
        timer_fired = [
            e
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED
        ]
        assert len(tp_advanced) == 1
        assert len(timer_fired) == 1
        assert tp_advanced[0].event_id < timer_fired[0].event_id
        assert tp_advanced[0].worker_may_ignore is True

        attrs = tp_advanced[0].workflow_execution_time_point_advanced_event_attributes
        dur = attrs.duration_advanced.ToTimedelta()
        assert timedelta(minutes=59) < dur < timedelta(hours=1, seconds=10)


async def test_manual_skip_single_timer(env: WorkflowEnvironment):
    ts = WorkflowTimeSkipper(env.client)
    async with new_worker(ts.client, SingleTimerWorkflow) as worker:
        handle = await ts.client.start_workflow(
            SingleTimerWorkflow.run,
            id=f"test-manual-skip-single-timer-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait for worker to process first WFT (schedules the timer)
        async def wft_completed():
            history = await handle.fetch_history()
            assert any(
                e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
                for e in history.events
            )

        await assert_eventually(wft_completed)

        # Describe — should have timer time point, offset near zero
        desc = await ts.describe(handle)
        assert desc.time_skipping is not None
        assert desc.time_skipping.config is not None
        assert desc.time_skipping.config.enabled is True
        assert desc.time_skipping.config.auto_skip is None
        assert desc.time_skipping.virtual_time_offset is not None
        assert desc.time_skipping.virtual_time_offset < timedelta(seconds=10)
        assert len(desc.upcoming_time_points) == 1
        assert desc.upcoming_time_points[0].raw.HasField("timer")
        upcoming_fire_time = desc.upcoming_time_points[0].fire_time

        # Advance — fires the 1h timer
        result = await ts.advance(handle)
        assert len(result.advanced_time_points) == 1
        assert result.advanced_time_points[0].raw.HasField("timer")
        # Fire time from advance matches what describe reported
        assert result.advanced_time_points[0].fire_time == upcoming_fire_time
        assert result.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(minutes=59)
            < result.time_skipping.virtual_time_offset
            < timedelta(hours=1, seconds=10)
        )
        assert len(result.upcoming_time_points) == 0

        # Workflow completes
        assert await handle.result() == "done"

        # History
        history = await handle.fetch_history()
        tp_advanced = [
            e
            for e in history.events
            if e.event_type
            == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_POINT_ADVANCED
        ]
        timer_fired = [
            e
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED
        ]
        assert len(tp_advanced) == 1
        assert len(timer_fired) == 1
        assert tp_advanced[0].event_id < timer_fired[0].event_id
        assert tp_advanced[0].worker_may_ignore is True

        attrs = tp_advanced[0].workflow_execution_time_point_advanced_event_attributes
        dur = attrs.duration_advanced.ToTimedelta()
        assert timedelta(minutes=59) < dur < timedelta(hours=1, seconds=10)


@activity.defn
async def advance_past_own_timeout() -> None:
    info = activity.info()
    assert info.workflow_id is not None
    ts = WorkflowTimeSkipper(activity.client())
    result = await ts.advance(info.workflow_id, run_id=info.workflow_run_id)
    # Verify the advance fired our schedule-to-close timeout
    assert len(result.advanced_time_points) == 1
    tp = result.advanced_time_points[0]
    assert tp.raw.HasField("activity_timeout")
    assert tp.raw.activity_timeout.timeout_type == int(TimeoutType.SCHEDULE_TO_CLOSE)
    # Fire time should match scheduled_time + schedule_to_close_timeout
    assert info.schedule_to_close_timeout is not None
    expected_fire_time = info.scheduled_time + info.schedule_to_close_timeout
    assert abs(tp.fire_time - expected_fire_time) < timedelta(seconds=5)


@workflow.defn
class ActivityTimeoutWorkflow:
    @workflow.run
    async def run(self) -> str:
        await workflow.execute_activity(
            advance_past_own_timeout,
            schedule_to_close_timeout=timedelta(hours=1),
        )
        return "done"


async def test_activity_advances_past_own_timeout(env: WorkflowEnvironment):
    ts = WorkflowTimeSkipper(env.client, auto_skip=True)
    async with new_worker(
        ts.client, ActivityTimeoutWorkflow, activities=[advance_past_own_timeout]
    ) as worker:
        handle = await ts.client.start_workflow(
            ActivityTimeoutWorkflow.run,
            id=f"test-activity-timeout-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Activity calls advance from inside, firing its own s2c timeout
        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()
        assert isinstance(exc_info.value.cause, ActivityError)
        assert isinstance(exc_info.value.cause.cause, TimeoutError)
        assert exc_info.value.cause.cause.type == TimeoutType.SCHEDULE_TO_CLOSE


@workflow.defn
class FourTimerWorkflow:
    @workflow.run
    async def run(self) -> str:
        import asyncio

        await asyncio.gather(
            workflow.sleep(timedelta(hours=1)),
            workflow.sleep(timedelta(hours=2)),
            workflow.sleep(timedelta(hours=3)),
            workflow.sleep(timedelta(hours=4)),
        )
        return "done"


@pytest.mark.parametrize("switch_mode", ["update_skipping", "advance_with_auto_skip"])
async def test_manual_then_auto_skip(env: WorkflowEnvironment, switch_mode: str):
    ts = WorkflowTimeSkipper(env.client)  # manual by default
    async with new_worker(ts.client, FourTimerWorkflow) as worker:
        handle = await ts.client.start_workflow(
            FourTimerWorkflow.run,
            id=f"test-manual-auto-{switch_mode}-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait for first WFT to complete (all 4 timers scheduled)
        async def wft_completed():
            history = await handle.fetch_history()
            assert any(
                e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
                for e in history.events
            )

        await assert_eventually(wft_completed)

        # Verify initial state: 4 upcoming, offset ~0
        desc = await ts.describe(handle)
        assert len(desc.upcoming_time_points) == 4
        assert desc.time_skipping is not None
        assert desc.time_skipping.virtual_time_offset is not None
        assert desc.time_skipping.virtual_time_offset < timedelta(seconds=10)

        # Manual advance — fires only the 1h timer
        result = await ts.advance(handle)
        assert len(result.advanced_time_points) == 1
        assert result.advanced_time_points[0].raw.HasField("timer")
        assert len(result.upcoming_time_points) == 3
        assert result.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(minutes=59)
            < result.time_skipping.virtual_time_offset
            < timedelta(hours=1, seconds=10)
        )

        # Wait for worker to process the advance WFT
        async def advance_processed():
            history = await handle.fetch_history()
            wft_completed_count = sum(
                1
                for e in history.events
                if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            )
            assert wft_completed_count >= 2  # initial + advance

        await assert_eventually(advance_processed)

        # Switch to auto-skip — fires remaining 3 (or 2) timers
        if switch_mode == "update_skipping":
            await ts.update_skipping(handle, auto_skip=True)
        else:
            # Advance fires 2h timer + enables auto-skip for the rest
            result = await ts.advance(handle, auto_skip=True)
            assert len(result.advanced_time_points) == 1
            assert len(result.upcoming_time_points) == 2

        # Workflow completes with all 4 timers fired
        assert await handle.result() == "done"

        # Final state: offset ~4h
        desc = await ts.describe(handle)
        assert desc.time_skipping is not None
        assert desc.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(hours=3, minutes=59)
            < desc.time_skipping.virtual_time_offset
            < timedelta(hours=4, seconds=10)
        )

        # History: 4 TIME_POINT_ADVANCED + 4 TIMER_FIRED
        history = await handle.fetch_history()
        tp_advanced = [
            e
            for e in history.events
            if e.event_type
            == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_POINT_ADVANCED
        ]
        timer_fired = [
            e
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED
        ]
        assert len(tp_advanced) == 4
        assert len(timer_fired) == 4


@workflow.defn
class WorkflowNowWorkflow:
    @workflow.run
    async def run(self) -> int:
        before = workflow.now()
        await workflow.sleep(timedelta(hours=1))
        after = workflow.now()
        return int((after - before).total_seconds() * 1000)


async def test_workflow_now_reflects_time_skip(env: WorkflowEnvironment):
    ts = WorkflowTimeSkipper(env.client, auto_skip=True)
    async with new_worker(ts.client, WorkflowNowWorkflow) as worker:
        handle = await ts.client.start_workflow(
            WorkflowNowWorkflow.run,
            id=f"test-workflow-now-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        result_ms = await handle.result()
        # workflow.now() difference should be ~1 hour (3_600_000 ms)
        assert 3_599_000 < result_ms < 3_610_000


@workflow.defn
class ContinueAsNewWorkflow:
    @workflow.run
    async def run(self) -> str:
        await workflow.sleep(timedelta(hours=1))
        if workflow.info().continued_run_id is None:
            # First run: continue-as-new
            workflow.continue_as_new()
        # Second run: return done
        return "done"


async def test_propagate_on_continue_as_new(env: WorkflowEnvironment):
    ts = WorkflowTimeSkipper(env.client, auto_skip=True)
    async with new_worker(ts.client, ContinueAsNewWorkflow) as worker:
        handle = await ts.client.start_workflow(
            ContinueAsNewWorkflow.run,
            id=f"test-can-propagate-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Auto-skip fires 1h timer in first run, CAN happens, auto-skip fires
        # 1h timer in second run, workflow completes with "done".
        assert await handle.result() == "done"

        # Describe the final (second) run.
        desc = await ts.describe(handle)
        assert desc.status == WorkflowExecutionStatus.COMPLETED
        assert desc.time_skipping is not None
        assert desc.time_skipping.config is not None
        assert desc.time_skipping.config.enabled is True
        assert desc.time_skipping.config.propagate_on_continue_as_new is True
        # Offset ~1h (only the second run's timer; offset resets on CAN).
        assert desc.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(minutes=59)
            < desc.time_skipping.virtual_time_offset
            < timedelta(hours=1, seconds=10)
        )

        # History of second run should have TIME_POINT_ADVANCED + TIMER_FIRED.
        history = await handle.fetch_history()
        tp_advanced = [
            e
            for e in history.events
            if e.event_type
            == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIME_POINT_ADVANCED
        ]
        timer_fired = [
            e
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_FIRED
        ]
        assert len(tp_advanced) == 1
        assert len(timer_fired) == 1


@workflow.defn
class SignalAndCaptureNowWorkflow:
    def __init__(self) -> None:
        self._signals_received = 0
        self._timestamps: list[float] = []

    @workflow.run
    async def run(self, num_signals: int) -> list[float]:
        # Capture initial workflow.now()
        self._timestamps.append(workflow.now().timestamp())
        for _ in range(num_signals):
            await workflow.wait_condition(
                lambda: self._signals_received > len(self._timestamps) - 1
            )
            self._timestamps.append(workflow.now().timestamp())
        return self._timestamps

    @workflow.signal
    async def my_signal(self) -> None:
        self._signals_received += 1


async def test_manual_advance_up_to_with_signals(env: WorkflowEnvironment):
    num_signals = 3
    ts = WorkflowTimeSkipper(env.client)
    async with new_worker(ts.client, SignalAndCaptureNowWorkflow) as worker:
        handle = await ts.client.start_workflow(
            SignalAndCaptureNowWorkflow.run,
            args=[num_signals],
            id=f"test-up-to-signals-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait for first WFT to complete (workflow is blocked on signal)
        async def wft_completed_count(n: int):
            async def check():
                history = await handle.fetch_history()
                count = sum(
                    1
                    for e in history.events
                    if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
                )
                assert count >= n

            return check

        await assert_eventually(await wft_completed_count(1))

        for i in range(num_signals):
            # Advance up_to 1 day (no time points to fire, just bumps offset)
            result = await ts.advance(handle, up_to=timedelta(days=1))
            assert len(result.advanced_time_points) == 0
            assert result.time_skipping.virtual_time_offset is not None
            expected_offset = timedelta(days=i + 1)
            assert (
                expected_offset - timedelta(seconds=10)
                < result.time_skipping.virtual_time_offset
                < expected_offset + timedelta(seconds=10)
            )

            # Send signal — triggers WFT, workflow captures now()
            await handle.signal(SignalAndCaptureNowWorkflow.my_signal)

            # Wait for the signal's WFT to complete
            await assert_eventually(await wft_completed_count(i + 2))

        # Workflow returns timestamps
        timestamps = await handle.result()
        assert len(timestamps) == num_signals + 1  # initial + one per signal

        # Each consecutive pair should be ~1 day apart
        for j in range(num_signals):
            diff = timestamps[j + 1] - timestamps[j]
            one_day_secs = 86400.0
            assert (
                one_day_secs - 10 < diff < one_day_secs + 10
            ), f"timestamps[{j+1}] - timestamps[{j}] = {diff}s, expected ~{one_day_secs}s"

        # Final offset should be ~3 days
        desc = await ts.describe(handle)
        assert desc.time_skipping is not None
        assert desc.time_skipping.virtual_time_offset is not None
        assert (
            timedelta(days=num_signals) - timedelta(seconds=10)
            < desc.time_skipping.virtual_time_offset
            < timedelta(days=num_signals) + timedelta(seconds=10)
        )
