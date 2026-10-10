import concurrent.futures
import threading

import pytest

import temporalio.exceptions
import temporalio.worker._activity


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_pending_cancel_is_delivered_on_thread_registration(
    cancel_before_start: bool,
) -> None:
    raiser = temporalio.worker._activity._ThreadExceptionRaiser()
    if cancel_before_start:
        raiser.raise_in_thread(temporalio.exceptions.CancelledError)

    executed = False

    def activity() -> None:
        nonlocal executed
        with raiser.active_thread():
            executed = True

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(activity)
        if cancel_before_start:
            with pytest.raises(temporalio.exceptions.CancelledError):
                future.result(timeout=5)
        else:
            future.result(timeout=5)

        assert executed is not cancel_before_start
        assert raiser._thread_id is None
        assert raiser._pending_exception is None
        assert (
            executor.submit(lambda: "next activity").result(timeout=5)
            == "next activity"
        )


def test_pending_cancel_respects_thread_shield() -> None:
    raiser = temporalio.worker._activity._ThreadExceptionRaiser()
    raiser.raise_in_thread(temporalio.exceptions.CancelledError)
    executed = False

    def activity() -> None:
        nonlocal executed
        with raiser.active_thread():
            executed = True
            assert raiser._pending_exception is temporalio.exceptions.CancelledError

    with raiser.shielded():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(activity).result(timeout=5)

    assert executed
    assert raiser._thread_id is None
    assert raiser._pending_exception is None


def test_cancel_during_activity_preserves_executor_thread() -> None:
    raiser = temporalio.worker._activity._ThreadExceptionRaiser()
    started = threading.Event()
    release = threading.Event()

    def activity() -> None:
        with raiser.active_thread():
            started.set()
            assert release.wait(timeout=5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(activity)
        try:
            assert started.wait(timeout=5)
            raiser.raise_in_thread(temporalio.exceptions.CancelledError)
        finally:
            release.set()

        with pytest.raises(temporalio.exceptions.CancelledError):
            future.result(timeout=5)

        assert raiser._thread_id is None
        assert (
            executor.submit(lambda: "next activity").result(timeout=5)
            == "next activity"
        )


def test_cancel_after_activity_preserves_executor_thread() -> None:
    raiser = temporalio.worker._activity._ThreadExceptionRaiser()

    def activity() -> str:
        with raiser.active_thread():
            return "first activity"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(activity).result(timeout=5) == "first activity"
        raiser.raise_in_thread(temporalio.exceptions.CancelledError)
        assert (
            executor.submit(lambda: "next activity").result(timeout=5)
            == "next activity"
        )
