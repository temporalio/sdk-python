from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Optional, Union

import google.protobuf.empty_pb2

import temporalio.api.testservice.v1
import temporalio.client
import temporalio.common
import temporalio.converter

logger = logging.getLogger(__name__)


class WorkflowEnvironment:
    @staticmethod
    def from_client(client: temporalio.client.Client) -> WorkflowEnvironment:
        return WorkflowEnvironment(client)

    @staticmethod
    async def start_time_skipping(
        *,
        auto_time_skipping: bool = True,
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Iterable[
            Union[
                temporalio.client.Interceptor,
                Callable[
                    [temporalio.client.OutboundInterceptor],
                    temporalio.client.OutboundInterceptor,
                ],
            ]
        ] = [],
        default_workflow_query_reject_condition: Optional[
            temporalio.common.QueryRejectCondition
        ] = None,
        retry_config: Optional[temporalio.client.RetryConfig] = None,
        rpc_metadata: Mapping[str, str] = {},
        identity: Optional[str] = None,
        test_server_stdout: Optional[Any] = subprocess.PIPE,
        test_server_stderr: Optional[Any] = subprocess.PIPE,
    ) -> TimeSkippingWorkflowEnvironment:
        # Download server binary. We accept this is not async.
        exe_path = _ensure_test_server_downloaded("1.15.1", Path(tempfile.gettempdir()))

        # Get a free port and start the server
        port = _get_free_port()
        args = [port]
        if auto_time_skipping:
            args.append("--enable-time-skipping")
        test_server_process = await asyncio.create_subprocess_exec(
            exe_path, *args, stdout=test_server_stdout, stderr=test_server_stderr
        )

        # We must terminate the process if we can't connect
        try:
            # Continually attempt to connect every 100ms for 5 seconds
            err_count = 0
            while True:
                try:
                    return TimeSkippingWorkflowEnvironment(
                        await temporalio.client.Client.connect(
                            f"localhost:{port}",
                            data_converter=data_converter,
                            interceptors=interceptors,
                            default_workflow_query_reject_condition=default_workflow_query_reject_condition,
                            retry_config=retry_config,
                            rpc_metadata=rpc_metadata,
                            identity=identity,
                        ),
                        test_server_process=test_server_process,
                        auto_time_skipping=auto_time_skipping,
                    )
                except RuntimeError as err:
                    err_count += 1
                    if err_count >= 50:
                        raise RuntimeError(
                            "Test server could not connect after 5 seconds"
                        ) from err
                    await asyncio.sleep(0.1)
        except Exception:
            if test_server_process.returncode is not None:
                try:
                    test_server_process.terminate()
                    await asyncio.wait_for(test_server_process.wait(), 5)
                except Exception:
                    logger.warning(
                        "Failed stopping test server on failure", exc_info=True
                    )
            raise

    def __init__(self, client: temporalio.client.Client) -> None:
        self._client = client

    async def __aenter__(self) -> WorkflowEnvironment:
        return self

    async def __aexit__(self, *args) -> None:
        await self.shutdown()

    @property
    def client(self) -> temporalio.client.Client:
        return self._client

    async def shutdown(self) -> None:
        pass

    async def sleep(self, duration: Union[timedelta, float]) -> None:
        await asyncio.sleep(
            duration.total_seconds() if isinstance(duration, timedelta) else duration
        )

    async def get_current_time(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def has_time_skipping(self) -> bool:
        return False

    @property
    def auto_time_skipping(self) -> bool:
        return False

    async def set_auto_time_skipping(self, auto_time_skipping: bool) -> bool:
        raise NotImplementedError

    @asynccontextmanager
    async def auto_time_skipping_disabled(self) -> AsyncIterator[None]:
        was_set = await self.set_auto_time_skipping(True)
        try:
            yield None
        finally:
            if was_set:
                await self.set_auto_time_skipping(False)


class TimeSkippingWorkflowEnvironment(WorkflowEnvironment):
    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        test_server_process: asyncio.subprocess.Process,
        auto_time_skipping: bool,
    ) -> None:
        super().__init__(client)
        self._test_server_process = test_server_process
        self._auto_time_skipping = auto_time_skipping

    @property
    def test_server_process(self) -> asyncio.subprocess.Process:
        return self._test_server_process

    async def shutdown(self) -> None:
        self._test_server_process.terminate()
        await self._test_server_process.wait()

    async def sleep(self, duration: Union[timedelta, float]) -> None:
        req = temporalio.api.testservice.v1.SleepRequest()
        req.duration.FromTimedelta(
            duration if isinstance(duration, timedelta) else timedelta(seconds=duration)
        )
        if self._auto_time_skipping:
            await self._client.test_service.sleep(req)
        else:
            await self._client.test_service.unlock_time_skipping_with_sleep(req)

    async def get_current_time(self) -> datetime:
        resp = await self._client.test_service.get_current_time(
            google.protobuf.empty_pb2.Empty()
        )
        return resp.time.ToDatetime().replace(tzinfo=timezone.utc)

    @property
    def has_time_skipping(self) -> bool:
        return True

    @property
    def auto_time_skipping(self) -> bool:
        return self._auto_time_skipping

    async def set_auto_time_skipping(self, auto_time_skipping: bool) -> bool:
        if self._auto_time_skipping and not auto_time_skipping:
            await self._client.test_service.lock_time_skipping(
                temporalio.api.testservice.v1.LockTimeSkippingRequest()
            )
            self._auto_time_skipping = False
            return True
        if not self._auto_time_skipping and auto_time_skipping:
            await self._client.test_service.unlock_time_skipping(
                temporalio.api.testservice.v1.UnlockTimeSkippingRequest()
            )
            self._auto_time_skipping = True
            return True
        return False


def _ensure_test_server_downloaded(version: str, dest_dir: Path) -> Path:
    # If already present, skip download
    if not dest_dir.is_dir():
        raise RuntimeError(f"Directory for test server, {dest_dir}, not present")

    # Build the URL
    plat = platform.system()
    ext = ".tar.gz"
    out_ext = ""
    if plat == "Windows":
        plat = "windows"
        ext = ".zip"
        out_ext = ".exe"
    elif plat == "Darwin":
        plat = "macOS"
    elif plat == "Linux":
        plat = "linux"
    else:
        raise RuntimeError(f"Unrecognized platform {plat}")

    # Ignore if already present
    dest = dest_dir / f"temporal-test-server-{version}{out_ext}"
    if dest.exists():
        return dest

    # We intentionally always choose amd64 even in cases of ARM processors
    # because we don't have native ARM binaries yet and some systems like M1 can
    # run the amd64 one.
    name = f"temporal-test-server_{version}_{plat}_amd64"
    url = f"https://github.com/temporalio/sdk-java/releases/download/v{version}/{name}{ext}"

    # Download to memory then extract single file to dest
    # TODO(cretz): Too expensive? Tests show it's quite cheap
    logger.info("Downloading %s to extract test server to %s", url, dest)
    with urllib.request.urlopen(url) as url_file:
        with BytesIO(url_file.read()) as comp_content:
            with (zipfile.ZipFile(comp_content) if ext == ".zip" else tarfile.open(fileobj=comp_content)) as comp_file:  # type: ignore
                with open(dest, "wb") as out_file:
                    shutil.copyfileobj(
                        comp_file.open(f"{name}/temporal-test-server{out_ext}"),
                        out_file,
                    )
    # If not an exe, we need make it executable
    if out_ext != ".exe":
        os.chmod(
            dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
    return dest


def _get_free_port() -> str:
    # Binds a port from the OS then closes the socket. Most OS's won't give the
    # same port back to the next socket right away even after this one is
    # closed.
    sock = socket.socket()
    try:
        sock.bind(("", 0))
        return str(sock.getsockname()[1])
    finally:
        sock.close()
