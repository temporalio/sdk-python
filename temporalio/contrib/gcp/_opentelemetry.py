"""OpenTelemetry plugin with Google Cloud Run defaults."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import (
    ProxyTracerProvider,
    TracerProvider,
    get_tracer_provider,
    set_tracer_provider,
)

from temporalio.contrib.opentelemetry import (
    OpenTelemetryPlugin as TemporalOpenTelemetryPlugin,
)
from temporalio.contrib.opentelemetry import create_tracer_provider
from temporalio.contrib.opentelemetry._tracer_provider import (
    ReplaySafeTracerProvider,
)
from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig
from temporalio.service import ConnectConfig, ServiceClient
from temporalio.worker import Worker

logger = logging.getLogger(__name__)

OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
"""Standard OpenTelemetry environment variable for the common OTLP endpoint."""

OTEL_SERVICE_NAME_ENV_VAR = "OTEL_SERVICE_NAME"
"""Standard OpenTelemetry environment variable for ``service.name``."""

CLOUD_RUN_WORKER_POOL_ENV_VAR = "CLOUD_RUN_WORKER_POOL"
"""Cloud Run environment variable containing the worker-pool name."""

CLOUD_RUN_SERVICE_ENV_VAR = "K_SERVICE"
"""Cloud Run environment variable containing the service name."""

DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
"""Default local OTLP gRPC collector endpoint."""

DEFAULT_SERVICE_NAME = "temporal-worker"
"""Service name used outside a recognized Cloud Run environment."""

DEFAULT_METRIC_PERIODICITY = timedelta(seconds=1)
"""Default interval between Temporal Core metric exports."""

DEFAULT_FLUSH_TIMEOUT = timedelta(seconds=10)
"""Default timeout for tracing force-flush operations."""


class OpenTelemetryPlugin(TemporalOpenTelemetryPlugin):
    """OpenTelemetry plugin for Temporal workers running on Google Cloud Run.

    The default configuration sends Temporal Core metrics and Python traces over
    OTLP gRPC to a collector on ``localhost:4317``. The collector is responsible
    for Google Cloud resource detection, authentication, and export.

    The plugin creates and installs a replay-safe global tracer provider unless
    ``tracer_provider`` is supplied. It also creates a Temporal runtime for Core
    metrics unless ``runtime`` is supplied. Call :py:meth:`shutdown` after every
    worker using the plugin has stopped.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        service_name: str | None = None,
        metric_periodicity: timedelta | None = None,
        flush_timeout: timedelta = DEFAULT_FLUSH_TIMEOUT,
        flush_on_worker_stop: bool = False,
        tracer_provider: TracerProvider | None = None,
        runtime: Runtime | None = None,
        add_temporal_spans: bool = False,
    ) -> None:
        """Initialize the Google Cloud OpenTelemetry plugin.

        Args:
            endpoint: OTLP gRPC endpoint. Falls back to
                ``OTEL_EXPORTER_OTLP_ENDPOINT``, then ``http://localhost:4317``.
            service_name: OpenTelemetry service name. Falls back to
                ``OTEL_SERVICE_NAME``, ``CLOUD_RUN_WORKER_POOL``, ``K_SERVICE``,
                then ``temporal-worker``.
            metric_periodicity: How often Temporal Core metrics are exported.
                Defaults to one second. Cannot be used with ``runtime``.
            flush_timeout: Default tracing force-flush timeout.
            flush_on_worker_stop: Whether to force-flush traces after each
                worker stops. Disabled by default because one plugin can be used
                by multiple workers.
            tracer_provider: Application-owned replay-safe tracer provider. It
                must have been created with
                :py:func:`temporalio.contrib.opentelemetry.create_tracer_provider`.
                When supplied, the plugin does not create an exporter or shut
                down the provider.
            runtime: Application-owned Temporal runtime. When supplied, the
                plugin does not create or modify Core metrics configuration. Use
                :py:func:`build_metrics_telemetry_config` to build a composable
                telemetry configuration for a custom runtime.
            add_temporal_spans: Whether the underlying Temporal OpenTelemetry
                plugin should add Temporal-specific operation spans.
        """
        _validate_positive_duration("flush_timeout", flush_timeout)
        if metric_periodicity is not None:
            _validate_positive_duration("metric_periodicity", metric_periodicity)
        if runtime is not None and metric_periodicity is not None:
            raise ValueError("metric_periodicity cannot be set with runtime")

        resolved_endpoint = _resolve_endpoint(endpoint, os.environ)
        resolved_service_name = _resolve_service_name(service_name, os.environ)
        resolved_metric_periodicity = (
            metric_periodicity
            if metric_periodicity is not None
            else DEFAULT_METRIC_PERIODICITY
        )

        owns_tracer_provider = tracer_provider is None
        if tracer_provider is None:
            resolved_tracer_provider = _create_tracer_provider(
                resolved_endpoint, resolved_service_name
            )
        elif isinstance(tracer_provider, ReplaySafeTracerProvider):
            resolved_tracer_provider = tracer_provider
        else:
            raise TypeError(
                "tracer_provider must be created with "
                "temporalio.contrib.opentelemetry.create_tracer_provider"
            )

        try:
            if runtime is None:
                runtime = Runtime(
                    telemetry=build_metrics_telemetry_config(
                        endpoint=resolved_endpoint,
                        service_name=resolved_service_name,
                        metric_periodicity=resolved_metric_periodicity,
                    )
                )
            _install_global_tracer_provider(resolved_tracer_provider)
        except BaseException:
            if owns_tracer_provider:
                resolved_tracer_provider.shutdown()
            raise

        self._endpoint = resolved_endpoint
        self._service_name = resolved_service_name
        self._flush_timeout = flush_timeout
        self._flush_on_worker_stop = flush_on_worker_stop
        self._tracer_provider = resolved_tracer_provider
        self._owns_tracer_provider = owns_tracer_provider
        self._runtime = runtime
        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self._shutdown_succeeded = True

        super().__init__(add_temporal_spans=add_temporal_spans)

    @property
    def endpoint(self) -> str:
        """Resolved OTLP collector endpoint."""
        return self._endpoint

    @property
    def service_name(self) -> str:
        """Resolved OpenTelemetry service name."""
        return self._service_name

    @property
    def tracer_provider(self) -> ReplaySafeTracerProvider:
        """Replay-safe tracer provider used by the plugin."""
        return self._tracer_provider

    @property
    def runtime(self) -> Runtime:
        """Temporal runtime used for Core metrics."""
        return self._runtime

    async def connect_service_client(
        self,
        config: ConnectConfig,
        next: Callable[[ConnectConfig], Awaitable[ServiceClient]],
    ) -> ServiceClient:
        """Install the metrics runtime before connecting the service client."""
        if config.runtime is not None and config.runtime is not self._runtime:
            raise ValueError(
                "OpenTelemetryPlugin runtime conflicts with the runtime passed "
                "to Client.connect; pass that runtime to the plugin instead"
            )
        config.runtime = self._runtime
        return await super().connect_service_client(config, next)

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        """Run a worker and optionally force-flush traces after it stops."""
        try:
            await super().run_worker(worker, next)
        finally:
            if self._flush_on_worker_stop:
                try:
                    succeeded = await asyncio.to_thread(
                        self.force_flush, self._flush_timeout
                    )
                    if not succeeded:
                        logger.warning(
                            "OpenTelemetry trace flush timed out after worker stop"
                        )
                except Exception:
                    logger.exception(
                        "OpenTelemetry trace flush failed after worker stop"
                    )

    def force_flush(self, timeout: timedelta | None = None) -> bool:
        """Export buffered Python traces without shutting down the provider.

        Temporal Core metrics are exported periodically and the Python runtime
        currently has no explicit metrics-flush API.

        Args:
            timeout: Maximum time to wait. Defaults to ``flush_timeout`` from
                the constructor.

        Returns:
            ``True`` when the tracer provider reports a successful flush.
        """
        resolved_timeout = timeout if timeout is not None else self._flush_timeout
        _validate_positive_duration("timeout", resolved_timeout)
        return self._tracer_provider.force_flush(
            _duration_to_milliseconds(resolved_timeout)
        )

    def shutdown(self, timeout: timedelta | None = None) -> bool:
        """Flush traces and shut down a provider created by the plugin.

        Application-owned tracer providers are force-flushed but are not shut
        down. This method is idempotent. Stop every worker using the plugin
        before calling it.

        Args:
            timeout: Maximum time allowed for the tracing force-flush. Provider
                shutdown happens after that flush.

        Returns:
            ``True`` when the tracing force-flush succeeded.
        """
        with self._shutdown_lock:
            if self._shutdown:
                return self._shutdown_succeeded

            succeeded = self.force_flush(timeout)
            if self._owns_tracer_provider:
                self._tracer_provider.shutdown()
            self._shutdown = True
            self._shutdown_succeeded = succeeded
            return succeeded


def build_metrics_telemetry_config(
    *,
    endpoint: str | None = None,
    service_name: str | None = None,
    metric_periodicity: timedelta | None = None,
) -> TelemetryConfig:
    """Build Temporal Core telemetry with Google Cloud Run defaults.

    This helper is useful when the application needs to customize runtime
    logging or other telemetry settings before constructing a
    :py:class:`temporalio.runtime.Runtime`.

    Args:
        endpoint: OTLP gRPC endpoint, with the same resolution order as
            :py:class:`OpenTelemetryPlugin`.
        service_name: Service name, with the same resolution order as
            :py:class:`OpenTelemetryPlugin`.
        metric_periodicity: Metric export interval. Defaults to one second.

    Returns:
        Telemetry configuration ready for a Temporal runtime.
    """
    resolved_periodicity = (
        metric_periodicity
        if metric_periodicity is not None
        else DEFAULT_METRIC_PERIODICITY
    )
    _validate_positive_duration("metric_periodicity", resolved_periodicity)
    resolved_endpoint = _resolve_endpoint(endpoint, os.environ)
    resolved_service_name = _resolve_service_name(service_name, os.environ)
    return TelemetryConfig(
        metrics=OpenTelemetryConfig(
            url=resolved_endpoint,
            metric_periodicity=resolved_periodicity,
        ),
        global_tags={"service_name": resolved_service_name},
    )


def _create_tracer_provider(
    endpoint: str, service_name: str
) -> ReplaySafeTracerProvider:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as err:
        raise RuntimeError(
            "The OTLP gRPC exporter is required. Install the "
            "'temporalio[gcp-opentelemetry]' extra."
        ) from err

    provider = create_tracer_provider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return provider


def _install_global_tracer_provider(provider: ReplaySafeTracerProvider) -> None:
    current = get_tracer_provider()
    if current is provider:
        return
    if not isinstance(current, ProxyTracerProvider):
        raise RuntimeError(
            "The global OpenTelemetry tracer provider is already configured. "
            "Pass that provider as tracer_provider if it was created with "
            "temporalio.contrib.opentelemetry.create_tracer_provider."
        )
    set_tracer_provider(provider)
    if get_tracer_provider() is not provider:
        raise RuntimeError("Failed to install the OpenTelemetry tracer provider")


def _resolve_endpoint(explicit: str | None, env: Mapping[str, str]) -> str:
    return (
        _non_empty(explicit)
        or _non_empty(env.get(OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR))
        or DEFAULT_OTLP_ENDPOINT
    )


def _resolve_service_name(explicit: str | None, env: Mapping[str, str]) -> str:
    return (
        _non_empty(explicit)
        or _non_empty(env.get(OTEL_SERVICE_NAME_ENV_VAR))
        or _non_empty(env.get(CLOUD_RUN_WORKER_POOL_ENV_VAR))
        or _non_empty(env.get(CLOUD_RUN_SERVICE_ENV_VAR))
        or DEFAULT_SERVICE_NAME
    )


def _non_empty(value: str | None) -> str | None:
    return value if value is not None and value.strip() else None


def _validate_positive_duration(name: str, value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


def _duration_to_milliseconds(value: timedelta) -> int:
    return max(1, round(value.total_seconds() * 1000))
