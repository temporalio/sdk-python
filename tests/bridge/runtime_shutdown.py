"""Exercise native callbacks during a real interpreter shutdown."""

import asyncio
import atexit
import pickle
import sys
import threading
from typing import Any

count = 1 if sys.argv[1] == "client" else int(sys.argv[1])
completed: list[bool] = []
release = threading.Event()
atexit.register(lambda: print("completed callbacks:", len(completed)))

# A native callback retains its function's globals. Keep them separate so it
# cannot keep the script's objects alive past interpreter finalization.
namespace: dict[str, Any] = {"completed": completed, "release": release}
exec(
    """
from temporalio.bridge.runtime import LoggingConfig, Runtime, RuntimeOptions, TelemetryConfig

def callback(entries, started):
    started.set()
    assert release.wait(10)
    completed.append(True)

def create_runtime(started):
    return Runtime(options=RuntimeOptions(telemetry=TelemetryConfig(
        logging=LoggingConfig(
            filter='INFO', forward_to=lambda entries: callback(entries, started), format=None),
        metrics=None)))
""",
    namespace,
)

if sys.argv[1] == "client":
    namespace["connect_config"], namespace["client_namespace"] = pickle.load(
        sys.stdin.buffer
    )
    exec(
        """
import asyncio
import threading
import temporalio.api.workflowservice.v1
from temporalio.client import Client

async def run_client():
    client = await Client.connect(namespace=client_namespace, **vars(connect_config))
    started = threading.Event()
    loop = asyncio.get_running_loop()
    original_call_soon = loop.call_soon_threadsafe

    def call_soon(*args, **kwargs):
        result = original_call_soon(*args, **kwargs)
        started.set()
        assert release.wait(10)
        completed.append(True)
        return result

    # Let Python receive the RPC result while its Rust completion callback
    # remains active, reproducing the client's interpreter-shutdown race.
    loop.call_soon_threadsafe = call_soon
    await client.workflow_service.describe_namespace(
        temporalio.api.workflowservice.v1.DescribeNamespaceRequest(namespace=client.namespace))
    assert started.wait(10)
""",
        namespace,
    )
    asyncio.run(namespace["run_client"]())
else:
    runtimes = []
    for _ in range(count):
        started = threading.Event()
        runtime = namespace["create_runtime"](started)
        runtimes.append(runtime)
        runtime.write_test_info_log("before interpreter shutdown", "")
        assert started.wait(10)

# Release runs first, followed by the SDK's shutdown hook, then the observer.
atexit.register(release.set)
