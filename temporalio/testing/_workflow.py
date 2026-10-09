"""Workflow test environment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    cast,
)

import google.protobuf.empty_pb2

import temporalio.api.common.v1
import temporalio.api.nexus.v1
import temporalio.api.operatorservice.v1
import temporalio.api.testservice.v1
import temporalio.bridge.testing
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.runtime
import temporalio.service
import temporalio.worker
from temporalio.testing._timeskipping import (
    TimeSkipper,
    TimeSkippingConfig,
)

logger = logging.getLogger(__name__)


class WorkflowEnvironment:
    """Workflow environment for testing workflows.

    Most developers will want to use one of these static methods:
    - :py:meth:`start_local`: start a local, full-featured Temporal server
    - :py:meth:`start_time_skipping_v2`: start a local, full-featured Temporal
      server with time-skipping enabled
    - :py:meth:`start_time_skipping`: start the older, featured-limited Java
      test server
    - :py:meth:`from_client`: use an existing server

    This environment is an async context manager, so it can be used with ``async
    with`` to make sure it shuts down properly. Otherwise, :py:meth:`shutdown`
    can be manually called.

    To use the environment, simply use the :py:attr:`client` on it.

    Workflows invoked on the workflow environment are automatically configured
    to have ``assert`` failures fail the workflow with the assertion error.
    """

    @classmethod
    def from_client(cls, client: temporalio.client.Client) -> WorkflowEnvironment:
        """Create a workflow environment from the given client.

        :py:attr:`supports_time_skipping_v1` and :py:attr:`supports_time_skipping_v2`
        will both return ``False`` for this environment.
        :py:meth:`sleep` will sleep the actual amount of time and
        :py:meth:`get_current_time` will return the current time.

        Args:
            client: The client to use for the environment.

        Returns:
            The workflow environment that runs against the given client.
        """
        return cls(_client_with_interceptors(client, _AssertionErrorInterceptor()))

    @classmethod
    async def start_local(
        cls,
        *,
        namespace: str = "default",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.DataConverter.default,
        interceptors: Sequence[temporalio.client.Interceptor] = [],
        plugins: Sequence[temporalio.client.Plugin] = [],
        default_workflow_query_reject_condition: None
        | (temporalio.common.QueryRejectCondition) = None,
        retry_config: temporalio.service.RetryConfig | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        identity: str | None = None,
        tls: bool | temporalio.service.TLSConfig = False,
        ip: str = "127.0.0.1",
        port: int | None = None,
        download_dest_dir: str | None = None,
        ui: bool = False,
        runtime: temporalio.runtime.Runtime | None = None,
        search_attributes: Sequence[temporalio.common.SearchAttributeKey] = (),
        dev_server_existing_path: str | None = None,
        dev_server_database_filename: str | None = None,
        dev_server_log_format: str = "pretty",
        dev_server_log_level: str | None = "warn",
        dev_server_download_version: str = "default",
        dev_server_extra_args: Sequence[str] = [],
        dev_server_download_ttl: timedelta | None = None,
        ui_port: int | None = None,
    ) -> WorkflowEnvironment:
        """Start a full Temporal server locally, downloading if necessary.

        Internally, this uses the Temporal CLI dev server from
        https://github.com/temporalio/cli. This is a self-contained binary for
        Temporal using Sqlite persistence. This call will download the CLI to a
        temporary directory by default if it has not already been downloaded
        before and ``dev_server_existing_path`` is not set.

        In the future, the dev server implementation may be changed to another
        implementation. Therefore, all ``dev_server_`` prefixed parameters are
        dev-server specific and may not apply to newer versions.

        Args:
            namespace: Namespace name to use for this environment.
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
            tls: See parameter of the same name on
                :py:meth:`temporalio.client.Client.connect`.
            ip: IP address to bind to, or 127.0.0.1 by default.
            port: Port number to bind to, or an OS-provided port by default.
            download_dest_dir: Directory to download binary to if a download is
                needed. If unset, this is the system's temporary directory.
            ui: If ``True``, will start a UI in the dev server.
            runtime: Specific runtime to use or default if unset.
            search_attributes: Search attributes to register with the dev
                server.
            dev_server_existing_path: Existing path to the CLI binary.
                If present, no download will be attempted to fetch the binary.
            dev_server_database_filename: Path to the Sqlite database to use
                for the dev server. Unset default means only in-memory Sqlite
                will be used.
            dev_server_log_format: Log format for the dev server.
            dev_server_log_level: Log level to use for the dev server. Default
                is ``warn``, but if set to ``None`` this will translate the
                Python logger's level to a dev server log level.
            dev_server_download_version: Specific CLI version to download.
                Defaults to ``default`` which downloads the version known to
                work best with this SDK.
            dev_server_extra_args: Extra arguments for the CLI binary.
            dev_server_download_ttl: TTL for the downloaded CLI binary. If unset, it will be
                cached indefinitely.
            ui_port: UI port to use if UI is enabled.

        Returns:
            The started CLI dev server workflow environment.
        """
        return await _NonTsWorkflowEnvironment._create(
            namespace=namespace,
            data_converter=data_converter,
            interceptors=interceptors,
            plugins=plugins,
            default_workflow_query_reject_condition=default_workflow_query_reject_condition,
            retry_config=retry_config,
            rpc_metadata=rpc_metadata,
            identity=identity,
            tls=tls,
            ip=ip,
            port=port,
            download_dest_dir=download_dest_dir,
            ui=ui,
            runtime=runtime,
            search_attributes=search_attributes,
            dev_server_existing_path=dev_server_existing_path,
            dev_server_database_filename=dev_server_database_filename,
            dev_server_log_format=dev_server_log_format,
            dev_server_log_level=dev_server_log_level,
            dev_server_download_version=dev_server_download_version,
            dev_server_extra_args=dev_server_extra_args,
            dev_server_download_ttl=dev_server_download_ttl,
            ui_port=ui_port,
        )

    @classmethod
    async def start_time_skipping(
        cls,
        *,
        data_converter: temporalio.converter.DataConverter = temporalio.converter.DataConverter.default,
        interceptors: Sequence[temporalio.client.Interceptor] = [],
        plugins: Sequence[temporalio.client.Plugin] = [],
        default_workflow_query_reject_condition: None
        | (temporalio.common.QueryRejectCondition) = None,
        retry_config: temporalio.service.RetryConfig | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        identity: str | None = None,
        port: int | None = None,
        download_dest_dir: str | None = None,
        runtime: temporalio.runtime.Runtime | None = None,
        test_server_existing_path: str | None = None,
        test_server_download_version: str = "default",
        test_server_extra_args: Sequence[str] = [],
        test_server_download_ttl: timedelta | None = None,
    ) -> WorkflowEnvironment:
        """Start a V1 time skipping workflow environment.

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

        In the future, the test server implementation may be changed to another
        implementation. Therefore, all ``test_server_`` prefixed parameters are
        test server specific and may not apply to newer versions.

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
            port: Port number to bind to, or an OS-provided port by default.
            download_dest_dir: Directory to download binary to if a download is
                needed. If unset, this is the system's temporary directory.
            runtime: Specific runtime to use or default if unset.
            test_server_existing_path: Existing path to the test server binary.
                If present, no download will be attempted to fetch the binary.
            test_server_download_version: Specific test server version to
                download. Defaults to ``default`` which downloads the version
                known to work best with this SDK.
            test_server_extra_args: Extra arguments for the test server binary.
            test_server_download_ttl: TTL for the downloaded test server binary. If unset, it
                will be cached indefinitely.

        Returns:
            The started workflow environment with time skipping.
        """
        return await _V1WorkflowEnvironment._create(
            data_converter=data_converter,
            interceptors=interceptors,
            plugins=plugins,
            default_workflow_query_reject_condition=default_workflow_query_reject_condition,
            retry_config=retry_config,
            rpc_metadata=rpc_metadata,
            identity=identity,
            port=port,
            download_dest_dir=download_dest_dir,
            runtime=runtime,
            test_server_existing_path=test_server_existing_path,
            test_server_download_version=test_server_download_version,
            test_server_extra_args=test_server_extra_args,
            test_server_download_ttl=test_server_download_ttl,
        )

    @classmethod
    async def start_time_skipping_v2(
        cls,
        *,
        ts_config: TimeSkippingConfig = TimeSkippingConfig(),
        **kwargs: Any,
    ) -> WorkflowEnvironment:
        """Start a local Temporal server with per-workflow time skipping enabled.

        Equivalent to :py:meth:`start_local` plus a :py:class:`TimeSkipper`
        wrap on the client, which stamps ``time_skipping_config`` on every
        workflow started via :py:attr:`client` and exposes
        :py:meth:`fast_forward` for driving time skipping on running
        workflows. Each workflow has its own virtual clock, unlike
        time-skipping V1.

        See :py:meth:`start_local` for other keyword arguments.
        """
        return await _V2WorkflowEnvironment._create(ts_config=ts_config, **kwargs)

    def __init__(self, client: temporalio.client.Client) -> None:
        """Create a workflow environment from a client.

        Most users would use a factory method instead.
        """
        self._client = client

    async def __aenter__(self) -> WorkflowEnvironment:
        """Noop for ``async with`` support."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """For ``async with`` support to just call :py:meth:`shutdown`."""
        await self.shutdown()

    @property
    def client(self) -> temporalio.client.Client:
        """Client to this environment."""
        return self._client

    async def connect_client(self, **kwargs: Any) -> temporalio.client.Client:
        """Create another client connected to this environment.

        Namespace and connection credentials from this environment's client are
        used by default.
        Keyword arguments are forwarded to :py:meth:`temporalio.client.Client.connect`
        and override those defaults.
        """
        config = self.client.service_client.config
        connect_kwargs: dict[str, Any] = {
            "namespace": self.client.namespace,
            "api_key": config.api_key,
            "tls": config.tls,
            "rpc_metadata": config.rpc_metadata,
            "runtime": config.runtime or temporalio.runtime.Runtime.default(),
        }
        connect_kwargs.update(kwargs)
        return await temporalio.client.Client.connect(
            config.target_host, **connect_kwargs
        )

    async def shutdown(self) -> None:
        """Shut down this environment."""
        pass

    async def sleep(self, duration: timedelta | float) -> None:
        """Sleep in this environment.

        This awaits a regular :py:func:`asyncio.sleep` in regular environments,
        or manually skips time in time-skipping environments.

        Args:
            duration: Amount of time to sleep.
        """
        await asyncio.sleep(
            duration.total_seconds() if isinstance(duration, timedelta) else duration
        )

    async def get_current_time(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any] | None = None,
    ) -> datetime:
        """Get the current time known to this environment.

        System time on non-time-skipping envs; the V1 test server's virtual
        clock on V1 envs. On V2 envs a ``handle`` is required — each
        workflow has its own virtual clock, read via
        ``TimeSkippingInfo.current_time``.

        Args:
            handle: On V2 envs, the workflow whose virtual clock to read.
                Ignored on non-V2 envs.
        """
        return datetime.now(timezone.utc)

    @property
    def supports_time_skipping_v1(self) -> bool:
        """True if this environment uses the V1 Java time-skipping server (which has limited server features)."""
        return False

    @property
    def supports_time_skipping_v2(self) -> bool:
        """True if this environment uses the V2 time-skipping server."""
        return False

    @property
    def supports_time_skipping(self) -> bool:
        """Whether this environment supports either V1 or V2 time skipping."""
        return self.supports_time_skipping_v1 or self.supports_time_skipping_v2

    async def create_nexus_endpoint(
        self, endpoint_name: str, task_queue: str
    ) -> temporalio.api.nexus.v1.Endpoint:
        """Create a Nexus endpoint with the given name and task queue.

        Args:
            endpoint_name: The name of the Nexus endpoint to create.
            task_queue: The task queue to associate with the endpoint.

        Returns:
            The created Nexus endpoint.
        """
        response = await self._client.operator_service.create_nexus_endpoint(
            temporalio.api.operatorservice.v1.CreateNexusEndpointRequest(
                spec=temporalio.api.nexus.v1.EndpointSpec(
                    name=endpoint_name,
                    target=temporalio.api.nexus.v1.EndpointTarget(
                        worker=temporalio.api.nexus.v1.EndpointTarget.Worker(
                            namespace=self._client.namespace,
                            task_queue=task_queue,
                        )
                    ),
                )
            )
        )
        return response.endpoint

    async def delete_nexus_endpoint(
        self, endpoint: temporalio.api.nexus.v1.Endpoint
    ) -> None:
        """Delete a Nexus endpoint.

        Args:
            endpoint: The Nexus endpoint to delete.
        """
        await self._client.operator_service.delete_nexus_endpoint(
            temporalio.api.operatorservice.v1.DeleteNexusEndpointRequest(
                id=endpoint.id,
                version=endpoint.version,
            )
        )

    @contextmanager
    def auto_time_skipping_disabled(self) -> Iterator[None]:
        """Disable V1's SDK-driven auto-unlock-on-result-await for the block.

        Only meaningful on time-skipping V1 envs. No-op on non-time-skipping
        envs. Unsupported on V2 envs — use :py:meth:`with_time_skipping_disabled`
        to suspend time-skipping config stamping on newly-started workflows
        instead.
        """
        yield None

    async def fast_forward(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        duration: timedelta | float | None = None,
        /,
    ) -> bool:
        """Fast-forward this workflow's virtual clock by ``duration``, and wait for it to complete.

        Only supported on time-skipping V2 environments (created via
        :py:meth:`start_time_skipping_v2`).

        Args:
            handle: Target workflow execution.
            duration: One-shot advance by this amount (``timedelta`` or
                seconds as a ``float``). If ``None``, skip time until workflow
                completion.

        Returns:
            True if the fast-forward transition is observed; False if the
            workflow terminates first.

        Raises:
            RuntimeError: If called on a V1 or non-time-skipping environment.
        """
        raise RuntimeError(
            "fast_forward requires a time-skipping environment; use "
            "WorkflowEnvironment.start_time_skipping_v2()."
        )

    @contextmanager
    def with_time_skipping_disabled(self) -> Iterator[None]:
        """Suspend V2 time-skipping config stamping on newly-started workflows within the block.

        Workflows started via :py:attr:`client` during the block do not
        receive a ``time_skipping_config`` on their start request. Existing
        workflows and V1 auto-behavior are unaffected. No-op on non-V2
        environments.
        """
        yield None

    async def get_time_skipping_info(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
    ) -> temporalio.api.common.v1.TimeSkippingInfo | None:
        """Fetch a workflow's ``TimeSkippingInfo`` via ``DescribeWorkflowExecution``.

        Returns ``None`` if time skipping has never been enabled on the
        workflow.

        Only supported on time-skipping V2 environments (created via
        :py:meth:`start_time_skipping_v2`).

        Raises:
            RuntimeError: If called on a V1 or non-time-skipping environment.
        """
        raise RuntimeError(
            "get_time_skipping_info requires a V2 time-skipping environment; "
            "use WorkflowEnvironment.start_time_skipping_v2()."
        )


class _HasAServer(WorkflowEnvironment):
    """Shared base for envs that own an ``EphemeralServer`` to shut down."""

    def __init__(
        self,
        client: temporalio.client.Client,
        server: temporalio.bridge.testing.EphemeralServer,
    ) -> None:
        super().__init__(client)
        self._server = server

    async def shutdown(self) -> None:
        await self._server.shutdown()

    @classmethod
    async def _bootstrap_dev_server(
        cls,
        *,
        namespace: str = "default",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.DataConverter.default,
        interceptors: Sequence[temporalio.client.Interceptor] = [],
        plugins: Sequence[temporalio.client.Plugin] = [],
        default_workflow_query_reject_condition: None
        | (temporalio.common.QueryRejectCondition) = None,
        retry_config: temporalio.service.RetryConfig | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        identity: str | None = None,
        tls: bool | temporalio.service.TLSConfig = False,
        ip: str = "127.0.0.1",
        port: int | None = None,
        download_dest_dir: str | None = None,
        ui: bool = False,
        runtime: temporalio.runtime.Runtime | None = None,
        search_attributes: Sequence[temporalio.common.SearchAttributeKey] = (),
        dev_server_existing_path: str | None = None,
        dev_server_database_filename: str | None = None,
        dev_server_log_format: str = "pretty",
        dev_server_log_level: str | None = "warn",
        dev_server_download_version: str = "default",
        dev_server_extra_args: Sequence[str] = [],
        dev_server_download_ttl: timedelta | None = None,
        ui_port: int | None = None,
    ) -> tuple[
        temporalio.bridge.testing.EphemeralServer,
        temporalio.client.Client,
    ]:
        """Start a CLI dev server and connect a client. Shared by non-ts and V2."""
        if not dev_server_log_level:
            if logger.isEnabledFor(logging.DEBUG):
                dev_server_log_level = "debug"
            elif logger.isEnabledFor(logging.INFO):
                dev_server_log_level = "info"
            elif logger.isEnabledFor(logging.WARNING):
                dev_server_log_level = "warn"
            elif logger.isEnabledFor(logging.ERROR):
                dev_server_log_level = "error"
            else:
                dev_server_log_level = "fatal"
        if search_attributes:
            new_args: list[str] = []
            for attr in search_attributes:
                new_args.append("--search-attribute")
                new_args.append(f"{attr.name}={attr._metadata_type}")
            new_args += dev_server_extra_args
            dev_server_extra_args = new_args

        runtime = runtime or temporalio.runtime.Runtime.default()
        download_ttl_ms = None
        if dev_server_download_ttl is not None:
            download_ttl_ms = int(dev_server_download_ttl.total_seconds() * 1000)
        server = await temporalio.bridge.testing.EphemeralServer.start_dev_server(
            runtime._core_runtime,
            temporalio.bridge.testing.DevServerConfig(
                existing_path=dev_server_existing_path,
                sdk_name="sdk-python",
                sdk_version=temporalio.service.__version__,
                download_version=dev_server_download_version,
                download_dest_dir=download_dest_dir,
                namespace=namespace,
                ip=ip,
                port=port,
                database_filename=dev_server_database_filename,
                ui=ui,
                ui_port=ui_port,
                log_format=dev_server_log_format,
                log_level=dev_server_log_level,
                extra_args=dev_server_extra_args,
                download_ttl_ms=download_ttl_ms,
            ),
        )

        try:
            client = await temporalio.client.Client.connect(
                server.target,
                namespace=namespace,
                data_converter=data_converter,
                interceptors=interceptors,
                plugins=plugins,
                default_workflow_query_reject_condition=default_workflow_query_reject_condition,
                tls=tls,
                retry_config=retry_config,
                rpc_metadata=rpc_metadata,
                identity=identity,
                runtime=runtime,
            )
        except:
            try:
                await server.shutdown()
            except:
                logger.warning(
                    "Failed stopping dev server on client connection failure",
                    exc_info=True,
                )
            raise
        return server, client


class _NonTsWorkflowEnvironment(_HasAServer):
    """Dev-server env with no time skipping."""

    def __init__(
        self,
        client: temporalio.client.Client,
        server: temporalio.bridge.testing.EphemeralServer,
    ) -> None:
        super().__init__(
            _client_with_interceptors(client, _AssertionErrorInterceptor()),
            server,
        )

    @classmethod
    async def _create(cls, **kwargs: Any) -> _NonTsWorkflowEnvironment:
        server, client = await cls._bootstrap_dev_server(**kwargs)
        return cls(client, server)


class _V1WorkflowEnvironment(_HasAServer):
    """Java test-server env with global-clock time skipping."""

    def __init__(
        self,
        client: temporalio.client.Client,
        server: temporalio.bridge.testing.EphemeralServer,
    ) -> None:
        self._auto_time_skipping = True
        super().__init__(
            _client_with_interceptors(
                client,
                _AssertionErrorInterceptor(),
                _TimeSkippingClientInterceptor(self),
            ),
            server,
        )

    @classmethod
    async def _create(
        cls,
        *,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[temporalio.client.Interceptor],
        plugins: Sequence[temporalio.client.Plugin],
        default_workflow_query_reject_condition: None
        | (temporalio.common.QueryRejectCondition),
        retry_config: temporalio.service.RetryConfig | None,
        rpc_metadata: Mapping[str, str | bytes],
        identity: str | None,
        port: int | None,
        download_dest_dir: str | None,
        runtime: temporalio.runtime.Runtime | None,
        test_server_existing_path: str | None,
        test_server_download_version: str,
        test_server_extra_args: Sequence[str],
        test_server_download_ttl: timedelta | None,
    ) -> _V1WorkflowEnvironment:
        runtime = runtime or temporalio.runtime.Runtime.default()
        download_ttl_ms = None
        if test_server_download_ttl:
            download_ttl_ms = int(test_server_download_ttl.total_seconds() * 1000)
        server = await temporalio.bridge.testing.EphemeralServer.start_test_server(
            runtime._core_runtime,
            temporalio.bridge.testing.TestServerConfig(
                existing_path=test_server_existing_path,
                sdk_name="sdk-python",
                sdk_version=temporalio.service.__version__,
                download_version=test_server_download_version,
                download_dest_dir=download_dest_dir,
                download_ttl_ms=download_ttl_ms,
                port=port,
                extra_args=test_server_extra_args,
            ),
        )
        try:
            client = await temporalio.client.Client.connect(
                server.target,
                data_converter=data_converter,
                interceptors=interceptors,
                plugins=plugins,
                default_workflow_query_reject_condition=default_workflow_query_reject_condition,
                retry_config=retry_config,
                rpc_metadata=rpc_metadata,
                identity=identity,
                runtime=runtime,
            )
        except:
            try:
                await server.shutdown()
            except:
                logger.warning(
                    "Failed stopping test server on client connection failure",
                    exc_info=True,
                )
            raise
        return cls(client, server)

    async def sleep(self, duration: timedelta | float) -> None:
        """Virtual-clock sleep via the V1 test server's ``test_service``."""
        req = temporalio.api.testservice.v1.SleepRequest()
        req.duration.FromTimedelta(
            duration if isinstance(duration, timedelta) else timedelta(seconds=duration)
        )
        await self._client.test_service.unlock_time_skipping_with_sleep(req)

    async def get_current_time(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any] | None = None,
    ) -> datetime:
        """V1 test server's virtual clock."""
        resp = await self._client.test_service.get_current_time(
            google.protobuf.empty_pb2.Empty()
        )
        return resp.time.ToDatetime().replace(tzinfo=timezone.utc)

    @property
    def supports_time_skipping_v1(self) -> bool:
        return True

    @contextmanager
    def auto_time_skipping_disabled(self) -> Iterator[None]:
        """Disable V1's SDK-driven auto-unlock-on-result-await for the block."""
        already_disabled = not self._auto_time_skipping
        self._auto_time_skipping = False
        try:
            yield None
        finally:
            if not already_disabled:
                self._auto_time_skipping = True

    @asynccontextmanager
    async def time_skipping_unlocked(self) -> AsyncIterator[None]:
        if not self._auto_time_skipping:
            yield None
            return
        await self._client.test_service.unlock_time_skipping(
            temporalio.api.testservice.v1.UnlockTimeSkippingRequest()
        )
        try:
            yield None
            await self._client.test_service.lock_time_skipping(
                temporalio.api.testservice.v1.LockTimeSkippingRequest()
            )
        except:
            try:
                await self._client.test_service.lock_time_skipping(
                    temporalio.api.testservice.v1.LockTimeSkippingRequest()
                )
            except:
                logger.exception("Failed locking time skipping after error")
            raise


class _V2WorkflowEnvironment(_HasAServer):
    """Dev-server env with per-workflow time skipping via ``TimeSkipper``."""

    def __init__(
        self,
        client: temporalio.client.Client,
        server: temporalio.bridge.testing.EphemeralServer,
        ts_config: TimeSkippingConfig,
    ) -> None:
        wrapped = _client_with_interceptors(client, _AssertionErrorInterceptor())
        self._ts_skipper = TimeSkipper(wrapped, config=ts_config)
        super().__init__(self._ts_skipper.client, server)

    @classmethod
    async def _create(
        cls,
        *,
        ts_config: TimeSkippingConfig,
        **kwargs: Any,
    ) -> _V2WorkflowEnvironment:
        server, client = await cls._bootstrap_dev_server(**kwargs)
        return cls(client, server, ts_config)

    async def sleep(self, duration: timedelta | float) -> None:
        """Unsupported on V2 — use :py:meth:`fast_forward` instead."""
        raise RuntimeError(
            "env.sleep is not supported in time-skipping V2 environments; use "
            "env.fast_forward(handle, duration) on a specific workflow."
        )

    async def get_current_time(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any] | None = None,
    ) -> datetime:
        """A workflow's current virtual time. Requires a ``handle`` on V2."""
        if handle is None:
            raise RuntimeError(
                "env.get_current_time requires a workflow handle in "
                "time-skipping V2 environments; each workflow has its "
                "own virtual clock."
            )
        return await self._ts_skipper.get_current_time(handle)

    @property
    def supports_time_skipping_v2(self) -> bool:
        return True

    @contextmanager
    def auto_time_skipping_disabled(self) -> Iterator[None]:
        """Unsupported on V2 — use :py:meth:`with_time_skipping_disabled` instead."""
        raise RuntimeError(
            "env.auto_time_skipping_disabled is not supported in "
            "time-skipping V2 environments; use "
            "env.with_time_skipping_disabled() to suspend time-skipping config "
            "stamping on newly-started workflows."
        )
        yield None  # unreachable; makes this a generator for @contextmanager

    async def fast_forward(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
        duration: timedelta | float | None = None,
        /,
    ) -> bool:
        return await self._ts_skipper.fast_forward(handle, duration)

    @contextmanager
    def with_time_skipping_disabled(self) -> Iterator[None]:
        with self._ts_skipper.with_time_skipping_disabled():
            yield None

    async def get_time_skipping_info(
        self,
        handle: temporalio.client.WorkflowHandle[Any, Any],
    ) -> temporalio.api.common.v1.TimeSkippingInfo | None:
        return await self._ts_skipper.get_time_skipping_info(handle)


class _AssertionErrorInterceptor(
    temporalio.client.Interceptor, temporalio.worker.Interceptor
):
    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[temporalio.worker.WorkflowInboundInterceptor] | None:
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


class _TimeSkippingClientInterceptor(temporalio.client.Interceptor):
    def __init__(self, env: _V1WorkflowEnvironment) -> None:  # type: ignore[reportMissingSuperCall]
        self.env = env

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _TimeSkippingClientOutboundInterceptor(next, self.env)


class _TimeSkippingClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        env: _V1WorkflowEnvironment,
    ) -> None:
        super().__init__(next)
        self.env = env

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        handle = cast(_TimeSkippingWorkflowHandle, await super().start_workflow(input))
        handle.__class__ = _TimeSkippingWorkflowHandle
        handle.env = self.env
        return handle


class _TimeSkippingWorkflowHandle(temporalio.client.WorkflowHandle):
    env: _V1WorkflowEnvironment  # type: ignore[reportUninitializedInstanceAttribute]

    async def result(
        self,
        *,
        follow_runs: bool = True,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
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
    config = client.config()
    config_interceptors = list(config["interceptors"])
    config_interceptors.extend(interceptors)
    config["interceptors"] = config_interceptors
    return temporalio.client.Client(**config)
