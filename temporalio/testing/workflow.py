"""Workflow test environment."""

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
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Type,
    Union,
    cast,
)

import google.protobuf.empty_pb2

import temporalio.api.testservice.v1
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.types
import temporalio.worker

logger = logging.getLogger(__name__)


class WorkflowEnvironment:
    """Workflow environment for testing workflows.

    Most developers will want to use the static :py:meth:`start_time_skipping`
    to start a test server process that automatically skips time as needed.

    This environment is an async context manager, so it can be used with
    ``async with`` to make sure it shuts down properly. Otherwise,
    :py:meth:`shutdown` can be manually called.

    To use the environment, simply use the :py:attr:`client` on it.

    Workflows invoked on the workflow environment are automatically configured
    to have ``assert`` failures fail the workflow with the assertion error.
    """

    @staticmethod
    def from_client(client: temporalio.client.Client) -> WorkflowEnvironment:
        """Create a workflow environment from the given client.

        :py:attr:`supports_time_skipping` will always return ``False`` for this
        environment. :py:meth:`sleep` will sleep the actual amount of time and
        :py:meth:`get_current_time` will return the current time.

        Args:
            client: The client to use for the environment.

        Returns:
            The workflow environment that runs against the given client.
        """
        # Add the assertion interceptor
        return WorkflowEnvironment(
            _client_with_interceptors(client, _AssertionErrorInterceptor())
        )

    @staticmethod
    async def start_time_skipping(
        *,
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
        test_server_exe_path: Optional[str] = None,
        test_server_version: str = "1.15.1",
    ) -> WorkflowEnvironment:
        """Start a time skipping workflow environment.

        By default, this environment will automatically skip to the next events
        in time when a workflow's
        :py:meth:`temporalio.client.WorkflowHandle.result` is awaited on (which
        includes :py:meth:`temporalio.client.Client.execute_workflow`). Before
        the result is awaited on, time can be manually skipped forward using
        :py:meth:`sleep`. The currently known time can be obtained via
        :py:meth:`get_current_time`.

        Internally, this environment lazily downloads a test-server binary for
        the current OS/arch into the temp directory if it is not already there.
        Then the executable is started and will be killed when
        :py:meth:`shutdown` is called (which is implicitly done if this is
        started via
        ``async with await WorkflowEnvironment.start_time_skipping()``).

        Users can reuse this environment for testing multiple independent
        workflows, but not concurrently. Time skipping, which is automatically
        done when awaiting a workflow result and manually done on
        :py:meth:`sleep`, is global to the environment, not to the workflow
        under test.

        Args:
            data_converter: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            interceptors: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            default_workflow_query_reject_condition: See parameter of the same
                name on :py:meth:`temporalio.client.Client.connect`.
            retry_config: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            rpc_metadata: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            identity: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            test_server_stdout: See ``stdout`` parameter on
                :py:func:`asyncio.loop.subprocess_exec`.
            test_server_stderr: See ``stderr`` parameter on
                :py:func:`asyncio.loop.subprocess_exec`.
            test_server_exe_path: Executable path for the test server. Defaults
                to a version-specific path in the temp directory, lazily
                downloaded at version ``test_server_version`` if not there.
            test_server_version: Test server version to lazily download when not
                found. This is only used if ``test_server_exe_path`` is not set.

        Returns:
            The started workflow environment with time skipping.
        """
        # Download server binary. We accept this is not async.
        exe_path = (
            Path(test_server_exe_path)
            if test_server_exe_path
            else _ensure_test_server_downloaded(
                test_server_version, Path(tempfile.gettempdir())
            )
        )

        # Get a free port and start the server
        port = _get_free_port()
        test_server_process = await asyncio.create_subprocess_exec(
            str(exe_path), port, stdout=test_server_stdout, stderr=test_server_stderr
        )

        # We must terminate the process if we can't connect
        try:
            # Continually attempt to connect every 100ms for 5 seconds
            err_count = 0
            while True:
                try:
                    return _TimeSkippingWorkflowEnvironment(
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
        """Create a workflow environment from a client.

        Most users would use a static method instead.
        """
        self._client = client

    async def __aenter__(self) -> WorkflowEnvironment:
        """Noop for ``async with`` support."""
        return self

    async def __aexit__(self, *args) -> None:
        """For ``async with`` support to just call :py:meth:`shutdown`."""
        await self.shutdown()

    @property
    def client(self) -> temporalio.client.Client:
        """Client to this environment."""
        return self._client

    async def shutdown(self) -> None:
        """Shut down this environment."""
        pass

    async def sleep(self, duration: Union[timedelta, float]) -> None:
        """Sleep in this environment.

        This awaits a regular :py:func:`asyncio.sleep` in regular environments,
        or manually skips time in time-skipping environments.

        Args:
            duration: Amount of time to sleep.
        """
        await asyncio.sleep(
            duration.total_seconds() if isinstance(duration, timedelta) else duration
        )

    async def get_current_time(self) -> datetime:
        """Get the current time known to this environment.

        For non-time-skipping environments this is simply the system time. For
        time-skipping environments this is whatever time has been skipped to.
        """
        return datetime.now(timezone.utc)

    @property
    def supports_time_skipping(self) -> bool:
        """Whether this environment supports time skipping."""
        return False

    @contextmanager
    def auto_time_skipping_disabled(self) -> Iterator[None]:
        """Disable any automatic time skipping if this is a time-skipping
        environment.

        This is a context manager for use via ``with``. Usually in time-skipping
        environments, waiting on a workflow result causes time to automatically
        skip until the next event. This can disable that. However, this only
        applies to results awaited inside this context. This will not disable
        automatic time skipping on previous results.

        This has no effect on non-time-skipping environments.
        """
        # It's always disabled for this base class
        yield None


class _AssertionErrorInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> Optional[Type[temporalio.worker.WorkflowInboundInterceptor]]:
        return _AssertionErrorWorkflowInboundInterceptor


class _AssertionErrorWorkflowInboundInterceptor(
    temporalio.worker.WorkflowInboundInterceptor
):
    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        with self.assert_error_as_app_error():
            return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with self.assert_error_as_app_error():
            return await super().handle_signal(input)

    @contextmanager
    def assert_error_as_app_error(self) -> Iterator[None]:
        try:
            yield None
        except AssertionError as err:
            app_err = temporalio.exceptions.ApplicationError(
                str(err), type="AssertionError", non_retryable=True
            )
            app_err.__traceback__ = err.__traceback__
            raise app_err from None


class _TimeSkippingWorkflowEnvironment(WorkflowEnvironment):
    def __init__(
        self,
        client: temporalio.client.Client,
        *,
        test_server_process: asyncio.subprocess.Process,
    ) -> None:
        # Add the assertion interceptor and time skipping interceptor
        super().__init__(
            _client_with_interceptors(
                client,
                _AssertionErrorInterceptor(),
                _TimeSkippingClientInterceptor(self),
            )
        )
        self.test_server_process = test_server_process
        self.auto_time_skipping = True

    async def shutdown(self) -> None:
        self.test_server_process.terminate()
        await self.test_server_process.wait()

    async def sleep(self, duration: Union[timedelta, float]) -> None:
        req = temporalio.api.testservice.v1.SleepRequest()
        req.duration.FromTimedelta(
            duration if isinstance(duration, timedelta) else timedelta(seconds=duration)
        )
        await self._client.test_service.unlock_time_skipping_with_sleep(req)

    async def get_current_time(self) -> datetime:
        resp = await self._client.test_service.get_current_time(
            google.protobuf.empty_pb2.Empty()
        )
        return resp.time.ToDatetime().replace(tzinfo=timezone.utc)

    @property
    def supports_time_skipping(self) -> bool:
        return True

    @contextmanager
    def auto_time_skipping_disabled(self) -> Iterator[None]:
        already_disabled = not self.auto_time_skipping
        self.auto_time_skipping = False
        try:
            yield None
        finally:
            if not already_disabled:
                self.auto_time_skipping = True

    @asynccontextmanager
    async def time_skipping_unlocked(self) -> AsyncIterator[None]:
        # If it's disabled, no locking/unlocking, just yield and return
        if not self.auto_time_skipping:
            yield None
            return
        # Unlock to start time skipping, lock again to stop it
        await self.client.test_service.unlock_time_skipping(
            temporalio.api.testservice.v1.UnlockTimeSkippingRequest()
        )
        try:
            yield None
            # Lock it back, throwing on error
            await self.client.test_service.lock_time_skipping(
                temporalio.api.testservice.v1.LockTimeSkippingRequest()
            )
        except:
            # Lock it back, swallowing error
            try:
                await self.client.test_service.lock_time_skipping(
                    temporalio.api.testservice.v1.LockTimeSkippingRequest()
                )
            except:
                logger.exception("Failed locking time skipping after error")
            raise


class _TimeSkippingClientInterceptor(temporalio.client.Interceptor):
    def __init__(self, env: _TimeSkippingWorkflowEnvironment) -> None:
        self.env = env

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _TimeSkippingClientOutboundInterceptor(next, self.env)


class _TimeSkippingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        env: _TimeSkippingWorkflowEnvironment,
    ) -> None:
        super().__init__(next)
        self.env = env

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        # We need to change the class of the handle so we can override result
        handle = cast(_TimeSkippingWorkflowHandle, await super().start_workflow(input))
        handle.__class__ = _TimeSkippingWorkflowHandle
        handle.env = self.env
        return handle


class _TimeSkippingWorkflowHandle(temporalio.client.WorkflowHandle):
    env: _TimeSkippingWorkflowEnvironment

    async def result(
        self,
        *,
        follow_runs: bool = True,
        rpc_metadata: Mapping[str, str] = {},
        rpc_timeout: Optional[timedelta] = None,
    ) -> Any:
        async with self.env.time_skipping_unlocked():
            return await super().result(
                follow_runs=follow_runs,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
            )


def _client_with_interceptors(
    client: temporalio.client.Client, *interceptors: temporalio.client.Interceptor
) -> temporalio.client.Client:
    # Shallow clone client and add interceptors
    config = client.config()
    config_interceptors = list(config["interceptors"])
    config_interceptors.extend(interceptors)
    config["interceptors"] = interceptors
    return temporalio.client.Client(**config)


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

    # Download to memory then extract single file to dest. Tests show this is
    # a cheap operation and no real need to put compressed file on disk first.
    logger.info("Downloading %s to extract test server to %s", url, dest)
    with urllib.request.urlopen(url) as url_file:
        with BytesIO(url_file.read()) as comp_content:
            with (zipfile.ZipFile(comp_content) if ext == ".zip" else tarfile.open(fileobj=comp_content)) as comp_file:  # type: ignore
                with open(dest, "wb") as out_file:
                    in_file = f"{name}/temporal-test-server{out_ext}"
                    shutil.copyfileobj(
                        comp_file.open(in_file)
                        if ext == ".zip"
                        else comp_file.extractfile(in_file),
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
