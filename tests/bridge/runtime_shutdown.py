"""Exercise native callbacks during a real interpreter shutdown."""

import asyncio
import atexit
import pickle
import sys
import threading

if __name__ == "__main__":
    release = threading.Event()
    completed: list[bool] = []
    atexit.register(lambda: print("completed callbacks:", len(completed)))

    # Import the bridge after registering the observer so that its shutdown
    # hook runs before the observer. Callbacks live in the helper module so
    # their globals do not keep this script's runtimes alive.
    import runtime_shutdown_helpers  # type: ignore[reportImplicitRelativeImport]

    # Release the callbacks before the bridge waits for them.
    atexit.register(release.set)

    if sys.argv[1] == "client":
        connect_config, namespace = pickle.load(sys.stdin.buffer)
        asyncio.run(
            runtime_shutdown_helpers.run_client(
                connect_config, namespace, release, completed
            )
        )
    else:
        runtimes = [
            runtime_shutdown_helpers.start_runtime(release, completed)
            for _ in range(int(sys.argv[1]))
        ]
