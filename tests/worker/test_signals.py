from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import cast
from unittest.mock import Mock

import pytest

import temporalio.client
import temporalio.worker
import temporalio.worker._signals as worker_signals

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signal handling")


@pytest.mark.requires_local_server
@pytest.mark.parametrize(
    "signum",
    [
        signal.SIGTERM,
        pytest.param(
            signal.SIGINT,
            marks=pytest.mark.skipif(
                sys.version_info < (3, 11),
                reason="asyncio.run cancels the main task on SIGINT since Python 3.11",
            ),
        ),
    ],
)
@pytest.mark.parametrize("mode", ["run", "context"])
@pytest.mark.parametrize("grace", ["complete", "expire"])
async def test_worker_os_signal_shutdown(
    client: temporalio.client.Client, signum: int, mode: str, grace: str
):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.worker.signal_worker",
        client.service_client.config.target_host,
        client.namespace,
        mode,
        grace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout
    lines: list[str] = []
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        assert line == b"READY\n", line
        process.send_signal(signum)
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        lines.append(line.decode())
        if signum == signal.SIGTERM:
            process.send_signal(signum)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        output = "".join(lines) + stdout.decode()
        assert process.returncode == 0, output + stderr.decode()
        assert "SHUTDOWN_COMPLETE" in output
        assert "BODY_CLEANED_UP" in output
        for index in range(2):
            assert f"SHUTDOWN_STARTED {index}" in output
            assert f"ACTIVITY_CLEANED_UP {index}" in output
            if grace == "complete":
                assert f"ACTIVITY_COMPLETED {index}" in output
                assert f"ACTIVITY_CANCELLED {index}" not in output
            else:
                assert f"ACTIVITY_CANCELLED {index}" in output
                assert f"ACTIVITY_COMPLETED {index}" not in output
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("handler", [signal.SIG_IGN, Mock()])
async def test_worker_sigterm_preserves_application_handler(
    handler: signal.Handlers | Callable[[int, FrameType | None], None],
):
    original = signal.signal(signal.SIGTERM, handler)
    try:
        with worker_signals._SigtermHandler(asyncio.Event(), None):
            assert signal.getsignal(signal.SIGTERM) is handler
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_preserves_asyncio_handler():
    loop = asyncio.get_running_loop()
    original = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, lambda: None)
    handler = signal.getsignal(signal.SIGTERM)
    try:
        with worker_signals._SigtermHandler(asyncio.Event(), None):
            assert signal.getsignal(signal.SIGTERM) is handler
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_scoped_registration():
    original = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    first = asyncio.Event()
    second = asyncio.Event()
    try:
        with worker_signals._SigtermHandler(second, None):
            with worker_signals._SigtermHandler(first, None):
                assert (
                    signal.getsignal(signal.SIGTERM) is worker_signals._handle_sigterm
                )
            worker_signals._handle_sigterm(signal.SIGTERM, None)
            await asyncio.sleep(0)
            assert not first.is_set()
            assert second.is_set()
            assert signal.getsignal(signal.SIGTERM) is worker_signals._handle_sigterm
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_preserves_replacement_handler():
    original = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with worker_signals._SigtermHandler(asyncio.Event(), None):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_ignores_exited_registration():
    original = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    shutdown = asyncio.Event()
    try:
        with worker_signals._SigtermHandler(shutdown, None):
            worker_signals._handle_sigterm(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert not shutdown.is_set()
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_cancels_shared_context_once():
    original = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    context_task = Mock()
    first = asyncio.Event()
    second = asyncio.Event()
    try:
        with (
            worker_signals._SigtermHandler(first, cast(asyncio.Task, context_task)),
            worker_signals._SigtermHandler(second, cast(asyncio.Task, context_task)),
        ):
            worker_signals._handle_sigterm(signal.SIGTERM, None)
            worker_signals._handle_sigterm(signal.SIGTERM, None)
            await asyncio.sleep(0)
            assert first.is_set() and second.is_set()
            context_task.cancel.assert_called_once_with()
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_worker_sigterm_non_main_thread():
    original = signal.getsignal(signal.SIGTERM)

    async def run_in_thread():
        with worker_signals._SigtermHandler(asyncio.Event(), None):
            assert signal.getsignal(signal.SIGTERM) is original

    await asyncio.to_thread(lambda: asyncio.run(run_in_thread()))
    assert signal.getsignal(signal.SIGTERM) is original


async def test_worker_sigterm_opt_out(client: temporalio.client.Client):
    from tests.worker.test_worker import never_run_activity

    original = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        async with temporalio.worker.Worker(
            client,
            task_queue="sigterm-opt-out",
            activities=[never_run_activity],
            shutdown_on_sigterm=False,
        ) as worker:
            while not worker.is_running:
                await asyncio.sleep(0.01)
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        assert worker.is_shutdown
    finally:
        signal.signal(signal.SIGTERM, original)
