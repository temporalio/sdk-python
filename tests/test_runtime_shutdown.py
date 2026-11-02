import socket
import subprocess
import sys
import uuid

import pytest

import temporalio.testing

# The registration wrapper only exposes the boundary of the real SDK exit hook.
# It lets the parent release a real native callback without timing-based sleeps.
_CHILD = r"""
import asyncio
import atexit
import contextlib
import logging
import socket
import sys
import threading

connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
send_lock = threading.Lock()

def send(message):
    with send_lock:
        connection.sendall((message + "\n").encode())

def user_finalizer():
    try:
        temporalio.runtime.Runtime(telemetry=temporalio.runtime.TelemetryConfig())
    except RuntimeError:
        send("late-runtime-rejected")
    else:
        raise AssertionError("SDK accepted a runtime after shutdown")
    send("python-finalizer")

atexit.register(user_finalizer)
register = atexit.register

def instrument_registration(callback, *args, **kwargs):
    def wrapped():
        send("shutdown-entered")
        callback(*args, **kwargs)
        send("shutdown-finished")
    register(wrapped)
    return callback

atexit.register = instrument_registration
try:
    import temporalio.runtime
    import temporalio.client
    import temporalio.api.enums.v1
    import temporalio.api.taskqueue.v1
    import temporalio.api.workflowservice.v1
finally:
    atexit.register = register

started = threading.Event()

def hold_callback():
    send("callback-started")
    started.set()
    if connection.recv(1) != b"r":
        raise AssertionError("Callback was not released")
    send("callback-finished")

class BlockingHandler(logging.Handler):
    def emit(self, record):
        if "shutdown-regression-log" in record.getMessage():
            if sys.argv[2] == "nested-rpc":
                asyncio.run(nested_poll())
            else:
                hold_callback()

class ControlledLoop(asyncio.SelectorEventLoop):
    held = False

    def call_soon_threadsafe(self, callback, *args, context=None):
        handle = super().call_soon_threadsafe(callback, *args, context=context)
        if threading.current_thread() is not threading.main_thread() and not self.held:
            self.held = True
            hold_callback()
        return handle

async def connect():
    await temporalio.client.Client.connect(sys.argv[3])
    if not started.wait(10):
        raise AssertionError("Native callback did not start")

async def start_poll():
    client = await temporalio.client.Client.connect(sys.argv[3])
    request = temporalio.api.workflowservice.v1.PollWorkflowTaskQueueRequest(
        namespace="default",
        task_queue=temporalio.api.taskqueue.v1.TaskQueue(
            name="shutdown-poll-" + sys.argv[1],
            kind=temporalio.api.enums.v1.TaskQueueKind.TASK_QUEUE_KIND_NORMAL,
        ),
        identity="shutdown-regression",
    )
    task = asyncio.create_task(client.workflow_service.poll_workflow_task_queue(request))
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready
    return task

async def nested_poll():
    # Connect before announcing the outstanding poll. This is a different
    # runtime from the log forwarder whose callback is awaiting this operation.
    task = await start_poll()
    send("callback-started")
    started.set()
    await task
    send("callback-finished")

async def cancel_poll():
    task = await start_poll()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

mode = sys.argv[2]
if mode in ("logging", "nested-rpc"):
    logger = logging.getLogger("runtime-shutdown")
    logger.setLevel(logging.INFO)
    logger.addHandler(BlockingHandler())
    runtime = temporalio.runtime.Runtime(
        telemetry=temporalio.runtime.TelemetryConfig(
            logging=temporalio.runtime.LoggingConfig(
                filter="INFO",
                forwarding=temporalio.runtime.LogForwardingConfig(logger=logger),
            )
        )
    )
    runtime._core_runtime.write_test_info_log("shutdown-regression-log", "")
    if not started.wait(10):
        raise AssertionError("Native log callback did not start")
elif mode == "rpc":
    loop = ControlledLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(connect())
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
else:
    asyncio.run(cancel_poll())
send("main-returned")
"""


@pytest.mark.requires_local_server
@pytest.mark.parametrize("mode", ["logging", "rpc", "cancelled-poll", "nested-rpc"])
async def test_runtime_shutdown(
    env: temporalio.testing.WorkflowEnvironment, mode: str
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(20)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD,
                str(listener.getsockname()[1]),
                mode,
                env.client.service_client.config.target_host,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        workflow_handle = None
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(20)
                with connection.makefile("rb") as messages:
                    if mode != "cancelled-poll":
                        assert messages.readline() == b"callback-started\n"
                    assert messages.readline() == b"main-returned\n"
                    assert messages.readline() == b"shutdown-entered\n"
                    if mode == "nested-rpc":
                        workflow_handle = await env.client.start_workflow(
                            "ShutdownPollWorkflow",
                            id=f"shutdown-{uuid.uuid4()}",
                            task_queue=f"shutdown-poll-{listener.getsockname()[1]}",
                        )
                        assert messages.readline() == b"callback-finished\n"
                    elif mode != "cancelled-poll":
                        connection.sendall(b"r")
                        assert messages.readline() == b"callback-finished\n"
                    assert messages.readline() == b"shutdown-finished\n"
                    assert messages.readline() == b"late-runtime-rejected\n"
                    assert messages.readline() == b"python-finalizer\n"
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, (stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            if workflow_handle is not None:
                await workflow_handle.terminate()
