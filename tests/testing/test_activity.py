import asyncio
import threading
from contextvars import copy_context

from temporalio import activity
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
            activity.heartbeat("cancelled")
        return "done"

    env = ActivityEnvironment()
    # Set heartbeat handler to add to list
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])
    # Start task and wait until waiting
    task = asyncio.create_task(env.run(do_stuff, "param1"))
    await waiting.wait()
    # Cancel and confirm done
    env.cancel()
    assert "done" == await task
    assert heartbeats == ["param: param1", "task, type: unknown", "cancelled"]


def test_activity_env_sync():
    waiting = threading.Event()

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
        activity.wait_for_cancelled_sync()
        activity.heartbeat("cancelled")

    env = ActivityEnvironment()
    # Set heartbeat handler to add to list
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])
    # Start thread and wait until waiting
    thread = threading.Thread(target=env.run, args=[do_stuff, "param1"])
    thread.start()
    waiting.wait()
    # Cancel and confirm done
    env.cancel()
    thread.join()
    assert heartbeats == ["param: param1", "task, type: unknown", "cancelled"]


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
