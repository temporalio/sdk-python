from threading import Event, Thread
from time import sleep

from temporalio.bridge.runtime import Runtime


class SomeException(Exception):
    pass


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
