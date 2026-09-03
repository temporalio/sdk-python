"""Tests for the standalone-activity operator commands: pause, unpause, reset and
update_options.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity
from temporalio.client import (
    ActivityExecutionStatus,
    ActivityHandle,
    ActivityOptionsKeys,
    Client,
    PendingActivityState,
)
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually

PAUSED_STATES = (PendingActivityState.PAUSED, PendingActivityState.PAUSE_REQUESTED)


@activity.defn
async def slow_activity() -> None:
    """Long-running activity that heartbeats and runs until cancellation."""
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.1)


@activity.defn
async def quick_activity() -> str:
    """Returns immediately. Used with a start delay so it can be paused while scheduled."""
    return "resumed"


@activity.defn
async def fail_then_succeed_activity() -> str:
    """Fails the first two attempts so retries are forced, then succeeds."""
    if activity.info().attempt < 3:
        raise ApplicationError("retryable failure")
    return "done"


@activity.defn
async def echo_activity(word: str) -> str:
    """Takes an argument and returns a value derived from it, so a completed execution
    has both an input and a successful outcome to read back off describe."""
    return f"{word}-echoed"


@activity.defn
async def always_fail_activity() -> None:
    """Always fails. Paired with a single-attempt retry policy so the activity reaches a
    terminal failure outcome rather than retrying."""
    raise ApplicationError("deliberate failure")


@activity.defn
async def heartbeat_fail_increment(value: int) -> int:
    """Heartbeats, fails the first attempt, then succeeds.

    The description will have input, a result, heartbeat details and a last failure.
    """
    activity.heartbeat("heartbeat details")
    if activity.info().attempt == 1:
        raise ApplicationError("deliberate first-attempt failure")
    return value + 1


@activity.defn
async def heartbeat_once_activity() -> None:
    """Records heartbeat details on attempt 1, then blocks waiting for cancellation."""
    if activity.info().attempt == 1:
        activity.heartbeat("hb-details")
    while True:
        await asyncio.sleep(0.1)


def _skip_if_unsupported(env: WorkflowEnvironment) -> None:
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support standalone activities")


async def _assert_eventually_paused(handle: ActivityHandle) -> None:
    async def check() -> None:
        desc = await handle.describe()
        assert desc.run_state in PAUSED_STATES

    await assert_eventually(check)


async def _start_running_slow_activity(
    client: Client, task_queue: str, **kwargs: Any
) -> ActivityHandle:
    """Start a slow activity and wait until it is actually running on the worker."""
    kwargs.setdefault("start_to_close_timeout", timedelta(seconds=60))
    kwargs.setdefault("heartbeat_timeout", timedelta(seconds=30))
    handle = await client.start_activity(
        slow_activity,
        id=f"act-{uuid.uuid4()}",
        task_queue=task_queue,
        **kwargs,
    )

    async def check() -> None:
        desc = await handle.describe()
        assert desc.run_state == PendingActivityState.STARTED

    await assert_eventually(check)
    return handle


async def test_unpause_resumes(client: Client, env: WorkflowEnvironment):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[quick_activity]):
        # Start delayed so the activity sits scheduled and can be paused before it runs.
        handle = await client.start_activity(
            quick_activity,
            id=f"act-{uuid.uuid4()}",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            start_delay=timedelta(seconds=30),
        )
        await handle.pause(reason="pause-before-unpause")

        # A not-yet-started (scheduled) activity transitions fully to PAUSED.
        async def check() -> None:
            desc = await handle.describe()
            assert desc.run_state == PendingActivityState.PAUSED

        await assert_eventually(check)

        await handle.unpause()

        async def resumed() -> None:
            desc = await handle.describe()
            assert desc.run_state not in PAUSED_STATES

        await assert_eventually(resumed)
        await handle.terminate(reason="cleanup")


async def test_reset(client: Client, env: WorkflowEnvironment):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(
        client, task_queue=task_queue, activities=[fail_then_succeed_activity]
    ):
        handle = await client.start_activity(
            fail_then_succeed_activity,
            id=f"act-{uuid.uuid4()}",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=0.2),
                backoff_coefficient=1.0,
                maximum_interval=timedelta(seconds=0.2),
                maximum_attempts=50,
            ),
        )

        async def has_retried() -> None:
            assert (await handle.describe()).attempt > 1

        await assert_eventually(has_retried)

        await handle.reset()

        # After reset the attempt counter goes back to the start.
        async def back_to_first_attempt() -> None:
            assert (await handle.describe()).attempt == 1

        await assert_eventually(back_to_first_attempt)
        await handle.terminate(reason="cleanup")


async def test_describe_paused_activity_reports_paused_status(
    client: Client, env: WorkflowEnvironment
):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[quick_activity]):
        # Start delayed so the activity sits scheduled; pausing from there reaches a true
        # PAUSED state rather than the PAUSE_REQUESTED of a running activity.
        handle = await client.start_activity(
            quick_activity,
            id=f"act-{uuid.uuid4()}",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            start_delay=timedelta(seconds=30),
        )
        assert (await handle.describe()).status == ActivityExecutionStatus.RUNNING

        await handle.pause(reason="hold")

        async def check() -> None:
            desc = await handle.describe()
            assert desc.status == ActivityExecutionStatus.PAUSED
            assert desc.run_state == PendingActivityState.PAUSED

        await assert_eventually(check)
        await handle.terminate(reason="cleanup")


async def test_update_options_respects_mask(client: Client, env: WorkflowEnvironment):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[slow_activity]):
        handle = await _start_running_slow_activity(
            client,
            task_queue,
            schedule_to_close_timeout=timedelta(seconds=120),
        )

        updated = await handle.update_options(
            [
                ActivityOptionsKeys.start_to_close_timeout.value_set(
                    timedelta(seconds=90)
                )
            ]
        )

        # Only start_to_close changed; schedule_to_close kept its original value.
        assert updated.start_to_close_timeout == timedelta(seconds=90)
        assert updated.schedule_to_close_timeout == timedelta(seconds=120)

        await handle.terminate(reason="cleanup")


async def test_update_options_all_fields(client: Client, env: WorkflowEnvironment):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[quick_activity]):
        # Start delayed so the activity stays scheduled while every option is updated.
        handle = await client.start_activity(
            quick_activity,
            id=f"act-{uuid.uuid4()}",
            task_queue=task_queue,
            schedule_to_close_timeout=timedelta(seconds=100),
            start_to_close_timeout=timedelta(seconds=30),
            start_delay=timedelta(seconds=300),
        )

        updated = await handle.update_options(
            [
                ActivityOptionsKeys.task_queue.value_set("updated-tq"),
                ActivityOptionsKeys.schedule_to_close_timeout.value_set(
                    timedelta(seconds=200)
                ),
                ActivityOptionsKeys.schedule_to_start_timeout.value_set(
                    timedelta(seconds=15)
                ),
                ActivityOptionsKeys.start_to_close_timeout.value_set(
                    timedelta(seconds=90)
                ),
                ActivityOptionsKeys.heartbeat_timeout.value_set(timedelta(seconds=25)),
                ActivityOptionsKeys.retry_policy.value_set(
                    RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        backoff_coefficient=2.0,
                        maximum_attempts=7,
                    )
                ),
                ActivityOptionsKeys.priority.value_set(Priority(priority_key=3)),
                ActivityOptionsKeys.start_delay.value_set(timedelta(seconds=500)),
            ]
        )

        assert updated.task_queue == "updated-tq"
        assert updated.schedule_to_close_timeout == timedelta(seconds=200)
        assert updated.schedule_to_start_timeout == timedelta(seconds=15)
        assert updated.start_to_close_timeout == timedelta(seconds=90)
        assert updated.heartbeat_timeout == timedelta(seconds=25)
        assert updated.retry_policy is not None
        assert updated.retry_policy.maximum_attempts == 7
        assert updated.priority is not None
        assert updated.priority.priority_key == 3
        assert updated.start_delay == timedelta(seconds=500)

        desc = await handle.describe()
        assert desc.task_queue == "updated-tq"
        await handle.terminate(reason="cleanup")


async def test_update_options_restore_original(
    client: Client, env: WorkflowEnvironment
):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[slow_activity]):
        handle = await _start_running_slow_activity(
            client, task_queue, start_to_close_timeout=timedelta(seconds=45)
        )

        changed = await handle.update_options(
            [
                ActivityOptionsKeys.start_to_close_timeout.value_set(
                    timedelta(seconds=90)
                )
            ]
        )
        assert changed.start_to_close_timeout == timedelta(seconds=90)

        # Restore alone reverts to the value the activity was created with.
        restored = await handle.restore_original_options()
        assert restored.start_to_close_timeout == timedelta(seconds=45)
        await handle.terminate(reason="cleanup")


async def test_update_options_on_paused_activity(
    client: Client, env: WorkflowEnvironment
):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[slow_activity]):
        handle = await _start_running_slow_activity(client, task_queue)
        await handle.pause(reason="hold")
        await _assert_eventually_paused(handle)

        # Updating options while paused applies, and leaves the activity paused.
        updated = await handle.update_options(
            [
                ActivityOptionsKeys.start_to_close_timeout.value_set(
                    timedelta(seconds=99)
                )
            ]
        )
        assert updated.start_to_close_timeout == timedelta(seconds=99)

        desc = await handle.describe()
        assert desc.run_state in PAUSED_STATES
        await handle.terminate(reason="cleanup")


async def test_reset_keeps_paused(client: Client, env: WorkflowEnvironment):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[slow_activity]):
        handle = await _start_running_slow_activity(client, task_queue)
        await handle.pause(reason="hold")
        await _assert_eventually_paused(handle)

        await handle.reset(keep_paused=True)

        # keep_paused leaves the activity paused after the reset.
        desc = await handle.describe()
        assert desc.run_state in PAUSED_STATES
        await handle.terminate(reason="cleanup")


async def test_reset_restores_original_options(
    client: Client, env: WorkflowEnvironment
):
    _skip_if_unsupported(env)
    task_queue = str(uuid.uuid4())
    async with Worker(client, task_queue=task_queue, activities=[quick_activity]):
        # Delayed start means the restore happens quickly.
        handle = await client.start_activity(
            quick_activity,
            id=f"act-{uuid.uuid4()}",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=45),
            start_delay=timedelta(seconds=300),
        )
        await handle.update_options(
            [ActivityOptionsKeys.task_queue.value_set("updated-tq")]
        )

        await handle.reset(restore_original_options=True)

        # Asserted through task_queue rather than a timeout: describe exposes the timeouts
        # only from sdk-python#1782 onward, and task_queue is equally round-tripped by the
        # restore.
        async def check() -> None:
            assert (await handle.describe()).task_queue == task_queue

        await assert_eventually(check)
        await handle.terminate(reason="cleanup")
