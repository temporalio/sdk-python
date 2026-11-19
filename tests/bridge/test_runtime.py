import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from time import sleep

import pytest

from temporalio.bridge.runtime import Runtime


class SomeException(Exception):
    pass


@pytest.mark.parametrize("runtime_count", [1, 3])
def test_bridge_runtime_shutdown_before_finalization(runtime_count: int):
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "faulthandler",
            str(Path(__file__).with_name("runtime_shutdown.py")),
            str(runtime_count),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"completed callbacks: {runtime_count}"
    assert result.stderr == ""


def test_bridge_runtime_raise_in_thread():
    waiting = Event()
    exc_in_thread: BaseException | None = None

    def wait_forever():
        try:
            waiting.set()
            while True:
                sleep(0.1)
        except BaseException as err:
            nonlocal exc_in_thread
            exc_in_thread = err

    # Start thread
    thread = Thread(target=wait_forever, daemon=True)
    thread.start()

    # Wait until sleeping
    waiting.wait(5)

    # Raise exception
    assert thread.ident
    assert thread.is_alive()
    assert Runtime._raise_in_thread(thread.ident, SomeException)

    # Make sure thread completes
    thread.join(5)
    assert not thread.is_alive()
    assert type(exc_in_thread) is SomeException
