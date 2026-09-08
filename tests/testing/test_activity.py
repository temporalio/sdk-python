import asyncio
import threading
import time
from contextvars import copy_context
from unittest.mock import Mock

import pytest

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import CancelledError
from temporalio.testing import ActivityEnvironment


async def test_activity_env_async():
    waiting = asyncio.Event()

    async def do_stuff(param: str) -> str:
        activity.heartbeat(f"param: {param}")

        # Ensure it works across create_task
        async def via_create_task():
            activity.heartbeat(f"task, type: {activity.info().activity_type}")

        await asyncio.create_task(via_create_task())

        # Wait for cancel
        try:
            waiting.set()
            await asyncio.Future()
            raise RuntimeError("Unreachable")
        except asyncio.CancelledError:
            cancellation_details = activity.cancellation_details()
            if cancellation_details:
                activity.heartbeat(
                    f"cancelled={cancellation_details.cancel_requested}",
                )
        return "done"

    env = ActivityEnvironment()
    # Set heartbeat handler to add to list
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])
    # Start task and wait until waiting
    task = asyncio.create_task(env.run(do_stuff, "param1"))
    await waiting.wait()
    # Cancel and confirm done
    env.cancel(
        cancellation_details=activity.ActivityCancellationDetails(cancel_requested=True)
    )
    assert "done" == await task
    assert heartbeats == ["param: param1", "task, type: unknown", "cancelled=True"]


def test_activity_env_sync():
    waiting = threading.Event()
    properly_cancelled = False

    def do_stuff(param: str) -> None:
        activity.heartbeat(f"param: {param}")

        # Ensure it works across thread
        context = copy_context()

        def via_thread():
            activity.heartbeat(f"task, type: {activity.info().activity_type}")

        thread = threading.Thread(target=context.run, args=[via_thread])
        thread.start()
        thread.join()

        # Wait for cancel
        waiting.set()
        try:
            # Confirm shielding works
            with activity.shield_thread_cancel_exception():
                try:
                    while not activity.is_cancelled():
                        time.sleep(0.2)
                    time.sleep(0.2)
                except:
                    raise RuntimeError("Unexpected")
        except CancelledError:
            nonlocal properly_cancelled
            cancellation_details = activity.cancellation_details()
            if cancellation_details:
                properly_cancelled = cancellation_details.cancel_requested
            else:
                properly_cancelled = False

    env = ActivityEnvironment()
    # Set heartbeat handler to add to list
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])
    # Start thread and wait until waiting
    thread = threading.Thread(target=env.run, args=[do_stuff, "param1"])
    thread.start()
    waiting.wait()
    # Cancel and confirm done
    time.sleep(1)
    env.cancel(
        cancellation_details=activity.ActivityCancellationDetails(cancel_requested=True)
    )
    thread.join()
    assert heartbeats == ["param: param1", "task, type: unknown"]
    assert properly_cancelled


async def test_activity_env_assert():
    async def assert_equals(a: str, b: str) -> None:
        assert a == b

    # Get out-of-env expected err
    try:
        await assert_equals("foo", "bar")
        assert False
    except Exception as err:
        expected_err = err

    # Get in-env actual err
    try:
        await ActivityEnvironment().run(assert_equals, "foo", "bar")
        assert False
    except Exception as err:
        actual_err = err

    assert type(expected_err) == type(actual_err)
    assert str(expected_err) == str(actual_err)


async def test_error_on_access_client_in_activity_environment_without_client():
    saw_error: bool = False

    async def my_activity() -> None:
        with pytest.raises(RuntimeError, match="No client available"):
            activity.client()
        nonlocal saw_error
        saw_error = True

    env = ActivityEnvironment()
    await env.run(my_activity)
    assert saw_error


async def test_access_client_in_activity_environment_with_client():
    got_client: bool = False

    async def my_activity() -> None:
        nonlocal got_client
        if activity.client():
            got_client = True

    env = ActivityEnvironment(client=Mock(spec=Client))
    await env.run(my_activity)
    assert got_client


async def test_error_on_access_client_in_sync_activity_in_environment_with_client():
    saw_error: bool = False

    def my_activity() -> None:
        with pytest.raises(RuntimeError, match="No client available"):
            activity.client()
        nonlocal saw_error
        saw_error = True

    env = ActivityEnvironment(client=Mock(spec=Client))
    env.run(my_activity)
    assert saw_error


async def test_activity_logger_does_not_mutate_caller_extra():
    """Regression test for https://github.com/temporalio/sdk-python/issues/503.

    The activity LoggerAdapter must not mutate the ``extra`` dict provided by
    the caller; it should merge its temporal context into a fresh dict.
    """
    import logging
    import logging.handlers
    import queue

    handler = logging.handlers.QueueHandler(queue.Queue())
    activity.logger.base_logger.addHandler(handler)
    previous_level = activity.logger.base_logger.level
    activity.logger.base_logger.setLevel(logging.INFO)
    previous_full = activity.logger.full_activity_info_on_extra
    activity.logger.full_activity_info_on_extra = True

    try:

        def log_with_extra() -> dict:
            caller_extra = {"request_id": "req-1"}
            activity.logger.info("hi", extra=caller_extra)
            return caller_extra

        env = ActivityEnvironment()
        caller_extra = env.run(log_with_extra)
    finally:
        activity.logger.base_logger.removeHandler(handler)
        activity.logger.base_logger.setLevel(previous_level)
        activity.logger.full_activity_info_on_extra = previous_full

    # The caller's dict must be untouched.
    assert caller_extra == {"request_id": "req-1"}

    # But the emitted record should still carry the temporal-injected fields
    # alongside the caller-provided one.
    records: list[logging.LogRecord] = list(handler.queue.queue)  # type: ignore[attr-defined]
    assert records, "expected one log record"
    record = records[-1]
    assert record.__dict__["request_id"] == "req-1"
    assert record.__dict__["temporal_activity"]["activity_type"] == "unknown"
    assert "activity_info" in record.__dict__
