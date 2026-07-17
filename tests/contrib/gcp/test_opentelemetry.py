"""Tests for the Google Cloud OpenTelemetry plugin."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from unittest.mock import Mock, call

import pytest
from opentelemetry.trace import NoOpTracerProvider, set_tracer_provider

from temporalio.client import ClientConfig
from temporalio.contrib.gcp import (
    CLOUD_RUN_SERVICE_ENV_VAR,
    CLOUD_RUN_WORKER_POOL_ENV_VAR,
    DEFAULT_METRIC_PERIODICITY,
    DEFAULT_OTLP_ENDPOINT,
    DEFAULT_SERVICE_NAME,
    OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR,
    OTEL_SERVICE_NAME_ENV_VAR,
    OpenTelemetryPlugin,
    build_metrics_telemetry_config,
)
from temporalio.contrib.opentelemetry import (
    OpenTelemetryInterceptor,
    create_tracer_provider,
)
from temporalio.contrib.opentelemetry._tracer_provider import (
    ReplaySafeTracerProvider,
)
from temporalio.runtime import OpenTelemetryConfig, Runtime
from temporalio.service import ConnectConfig, ServiceClient
from temporalio.worker import Worker


@pytest.fixture(autouse=True)
def _reset_global_provider(  # pyright: ignore[reportUnusedFunction]
    reset_otel_tracer_provider: None,  # pyright: ignore[reportUnusedParameter]
) -> None:
    pass


@pytest.fixture
def tracer_provider() -> ReplaySafeTracerProvider:
    return create_tracer_provider(shutdown_on_exit=False)


def _application_owned_plugin(
    tracer_provider: ReplaySafeTracerProvider,
    **kwargs: Any,
) -> OpenTelemetryPlugin:
    return OpenTelemetryPlugin(
        tracer_provider=tracer_provider,
        runtime=cast(Runtime, Mock(spec=Runtime)),
        **kwargs,
    )


def _clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR,
        OTEL_SERVICE_NAME_ENV_VAR,
        CLOUD_RUN_WORKER_POOL_ENV_VAR,
        CLOUD_RUN_SERVICE_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_and_plugin_integration(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_environment(monkeypatch)

    plugin = _application_owned_plugin(tracer_provider)

    assert plugin.endpoint == DEFAULT_OTLP_ENDPOINT
    assert plugin.service_name == DEFAULT_SERVICE_NAME
    assert plugin.tracer_provider is tracer_provider
    assert plugin.name() == "OpenTelemetryPlugin"

    config = plugin.configure_client(
        cast(ClientConfig, cast(object, {"interceptors": []}))
    )
    assert len(config["interceptors"]) == 1
    assert isinstance(config["interceptors"][0], OpenTelemetryInterceptor)


def test_resolution_precedence(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv(CLOUD_RUN_SERVICE_ENV_VAR, "cloud-run-service")
    plugin = _application_owned_plugin(tracer_provider)
    assert plugin.service_name == "cloud-run-service"

    monkeypatch.setenv(CLOUD_RUN_WORKER_POOL_ENV_VAR, "worker-pool")
    plugin = _application_owned_plugin(tracer_provider)
    assert plugin.service_name == "worker-pool"

    monkeypatch.setenv(OTEL_SERVICE_NAME_ENV_VAR, "otel-service")
    monkeypatch.setenv(OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR, "http://collector:4317")
    plugin = _application_owned_plugin(tracer_provider)
    assert plugin.service_name == "otel-service"
    assert plugin.endpoint == "http://collector:4317"

    plugin = _application_owned_plugin(
        tracer_provider,
        endpoint="https://explicit-collector:4317",
        service_name="explicit-service",
    )
    assert plugin.service_name == "explicit-service"
    assert plugin.endpoint == "https://explicit-collector:4317"


def test_ignores_empty_environment_values(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OTEL_SERVICE_NAME_ENV_VAR, " ")
    monkeypatch.setenv(CLOUD_RUN_WORKER_POOL_ENV_VAR, "")
    monkeypatch.setenv(CLOUD_RUN_SERVICE_ENV_VAR, "cloud-run-service")

    plugin = _application_owned_plugin(tracer_provider)

    assert plugin.service_name == "cloud-run-service"


def test_build_metrics_telemetry_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv(CLOUD_RUN_WORKER_POOL_ENV_VAR, "worker-pool")
    config = build_metrics_telemetry_config(
        endpoint="http://collector:4317",
        metric_periodicity=timedelta(seconds=30),
    )

    assert isinstance(config.metrics, OpenTelemetryConfig)
    assert config.metrics.url == "http://collector:4317"
    assert config.metrics.metric_periodicity == timedelta(seconds=30)
    assert config.global_tags == {"service_name": "worker-pool"}


@pytest.mark.asyncio
async def test_connect_service_client_installs_runtime(
    tracer_provider: ReplaySafeTracerProvider,
) -> None:
    runtime = cast(Runtime, Mock(spec=Runtime))
    plugin = OpenTelemetryPlugin(
        tracer_provider=tracer_provider,
        runtime=runtime,
    )
    config = ConnectConfig(target_host="localhost:7233")
    service_client = cast(ServiceClient, Mock(spec=ServiceClient))

    async def connect(input: ConnectConfig) -> ServiceClient:
        assert input.runtime is runtime
        return service_client

    assert await plugin.connect_service_client(config, connect) is service_client

    conflicting_config = ConnectConfig(
        target_host="localhost:7233",
        runtime=cast(Runtime, Mock(spec=Runtime)),
    )
    with pytest.raises(ValueError, match="runtime conflicts"):
        await plugin.connect_service_client(conflicting_config, connect)


def test_force_flush_and_application_owned_shutdown(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_flush = Mock(return_value=True)
    shutdown = Mock()
    monkeypatch.setattr(tracer_provider, "force_flush", force_flush)
    monkeypatch.setattr(tracer_provider, "shutdown", shutdown)
    plugin = _application_owned_plugin(tracer_provider)

    assert plugin.force_flush(timedelta(seconds=2))
    assert plugin.shutdown(timedelta(seconds=3))
    assert plugin.shutdown(timedelta(seconds=4))

    assert force_flush.call_args_list == [call(2000), call(3000)]
    shutdown.assert_not_called()


def test_plugin_owned_provider_is_shut_down(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_flush = Mock(return_value=True)
    shutdown = Mock()
    monkeypatch.setattr(tracer_provider, "force_flush", force_flush)
    monkeypatch.setattr(tracer_provider, "shutdown", shutdown)
    monkeypatch.setattr(
        "temporalio.contrib.gcp._opentelemetry._create_tracer_provider",
        lambda endpoint, service_name: tracer_provider,
    )
    runtime = cast(Runtime, Mock(spec=Runtime))
    runtime_factory = Mock(return_value=runtime)
    monkeypatch.setattr(
        "temporalio.contrib.gcp._opentelemetry.Runtime", runtime_factory
    )

    plugin = OpenTelemetryPlugin()

    assert plugin.runtime is runtime
    telemetry = runtime_factory.call_args.kwargs["telemetry"]
    assert isinstance(telemetry.metrics, OpenTelemetryConfig)
    assert telemetry.metrics.url == DEFAULT_OTLP_ENDPOINT
    assert telemetry.metrics.metric_periodicity == DEFAULT_METRIC_PERIODICITY
    assert telemetry.global_tags == {"service_name": DEFAULT_SERVICE_NAME}
    assert plugin.shutdown(timedelta(seconds=2))
    force_flush.assert_called_once_with(2000)
    shutdown.assert_called_once_with()


def test_plugin_owned_provider_is_cleaned_up_on_runtime_failure(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown = Mock()
    monkeypatch.setattr(tracer_provider, "shutdown", shutdown)
    monkeypatch.setattr(
        "temporalio.contrib.gcp._opentelemetry._create_tracer_provider",
        lambda endpoint, service_name: tracer_provider,
    )
    monkeypatch.setattr(
        "temporalio.contrib.gcp._opentelemetry.Runtime",
        Mock(side_effect=RuntimeError("runtime failed")),
    )

    with pytest.raises(RuntimeError, match="runtime failed"):
        OpenTelemetryPlugin()

    shutdown.assert_called_once_with()


def test_rejects_conflicting_global_tracer_provider(
    tracer_provider: ReplaySafeTracerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_provider = create_tracer_provider(shutdown_on_exit=False)
    set_tracer_provider(existing_provider)
    shutdown = Mock()
    monkeypatch.setattr(tracer_provider, "shutdown", shutdown)
    monkeypatch.setattr(
        "temporalio.contrib.gcp._opentelemetry._create_tracer_provider",
        lambda endpoint, service_name: tracer_provider,
    )

    with pytest.raises(RuntimeError, match="already configured"):
        OpenTelemetryPlugin(runtime=cast(Runtime, Mock(spec=Runtime)))

    shutdown.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_on_worker_stop", [False, True])
async def test_worker_stop_flush_is_opt_in(
    tracer_provider: ReplaySafeTracerProvider,
    monkeypatch: pytest.MonkeyPatch,
    flush_on_worker_stop: bool,
) -> None:
    calls: list[str] = []

    def record_flush(_timeout_millis: int) -> bool:
        calls.append("flush")
        return True

    force_flush = Mock(side_effect=record_flush)
    monkeypatch.setattr(tracer_provider, "force_flush", force_flush)
    plugin = _application_owned_plugin(
        tracer_provider,
        flush_on_worker_stop=flush_on_worker_stop,
        flush_timeout=timedelta(seconds=2),
    )

    async def run(_worker: Worker) -> None:
        calls.append("run")

    await plugin.run_worker(cast(Worker, Mock(spec=Worker)), run)

    assert calls == (["run", "flush"] if flush_on_worker_stop else ["run"])


def test_validates_options(tracer_provider: ReplaySafeTracerProvider) -> None:
    with pytest.raises(ValueError, match="flush_timeout must be positive"):
        _application_owned_plugin(tracer_provider, flush_timeout=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="metric_periodicity cannot be set"):
        _application_owned_plugin(
            tracer_provider, metric_periodicity=timedelta(seconds=1)
        )
    with pytest.raises(ValueError, match="metric_periodicity must be positive"):
        build_metrics_telemetry_config(metric_periodicity=timedelta(0))

    plugin = _application_owned_plugin(tracer_provider)
    with pytest.raises(ValueError, match="timeout must be positive"):
        plugin.force_flush(timedelta(0))

    with pytest.raises(TypeError, match="create_tracer_provider"):
        OpenTelemetryPlugin(
            tracer_provider=NoOpTracerProvider(),
            runtime=cast(Runtime, Mock(spec=Runtime)),
        )
