"""Tests for temporalio.contrib.aws.lambda_worker.otel."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio.contrib.aws.lambda_worker._configure import (
    LambdaWorkerConfig,
    _run_shutdown_hooks,
)
from temporalio.contrib.aws.lambda_worker.otel import (
    OtelOptions,
    apply_defaults,
    apply_tracing,
    build_metrics_telemetry_config,
)
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.runtime import OpenTelemetryConfig, TelemetryConfig


def _make_tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return tracer_provider, span_exporter


class TestApplyTracing:
    def test_adds_interceptor(self) -> None:
        config = LambdaWorkerConfig()
        tracer_provider, _ = _make_tracer_provider()
        apply_tracing(config, tracer_provider)
        interceptors = config.client_connect_config.get("interceptors", [])
        assert len(interceptors) == 1

    def test_appends_to_existing_interceptors(self) -> None:
        config = LambdaWorkerConfig()
        # Use a TracingInterceptor as the existing interceptor.
        existing_provider, _ = _make_tracer_provider()
        existing = TracingInterceptor(tracer=existing_provider.get_tracer("existing"))
        config.client_connect_config["interceptors"] = [existing]
        tracer_provider, _ = _make_tracer_provider()
        apply_tracing(config, tracer_provider)
        interceptors = config.client_connect_config["interceptors"]
        assert len(interceptors) == 2
        assert interceptors[0] is existing

    def test_registers_flush_shutdown_hook(self) -> None:
        config = LambdaWorkerConfig()
        tracer_provider, _ = _make_tracer_provider()
        apply_tracing(config, tracer_provider)
        assert len(config.shutdown_hooks) == 1

    @pytest.mark.asyncio
    async def test_shutdown_hook_flushes(self) -> None:
        config = LambdaWorkerConfig()
        tracer_provider, _ = _make_tracer_provider()
        apply_tracing(config, tracer_provider)
        await _run_shutdown_hooks(config)


class TestBuildMetricsTelemetryConfig:
    def test_returns_telemetry_config(self) -> None:
        tc = build_metrics_telemetry_config(endpoint="http://localhost:4317")
        assert isinstance(tc, TelemetryConfig)
        assert isinstance(tc.metrics, OpenTelemetryConfig)
        assert tc.metrics.url == "http://localhost:4317"

    def test_default_endpoint(self) -> None:
        tc = build_metrics_telemetry_config()
        assert isinstance(tc.metrics, OpenTelemetryConfig)
        assert tc.metrics.url == "http://localhost:4317"

    def test_service_name_as_global_tag(self) -> None:
        tc = build_metrics_telemetry_config(service_name="my-svc")
        assert tc.global_tags.get("service_name") == "my-svc"

    def test_no_service_name_no_tag(self) -> None:
        tc = build_metrics_telemetry_config()
        assert "service_name" not in tc.global_tags

    def test_metric_periodicity(self) -> None:
        tc = build_metrics_telemetry_config(metric_periodicity=timedelta(seconds=30))
        assert isinstance(tc.metrics, OpenTelemetryConfig)
        assert tc.metrics.metric_periodicity == timedelta(seconds=30)

    def test_composable_with_custom_runtime(self) -> None:
        """User can compose the returned config into a custom Runtime."""
        import dataclasses

        tc = build_metrics_telemetry_config(endpoint="http://localhost:4317")
        # Replace logging config to demonstrate composability.
        custom_tc = dataclasses.replace(tc, logging=None)
        assert custom_tc.logging is None
        assert isinstance(custom_tc.metrics, OpenTelemetryConfig)


class TestApplyDefaults:
    def test_configures_metrics_and_tracing(self) -> None:
        config = LambdaWorkerConfig()
        apply_defaults(config, OtelOptions(collector_endpoint="http://localhost:4317"))

        assert "runtime" in config.client_connect_config
        interceptors = config.client_connect_config.get("interceptors", [])
        assert len(interceptors) == 1
        assert len(config.shutdown_hooks) == 1

    def test_service_name_from_options(self) -> None:
        config = LambdaWorkerConfig()
        apply_defaults(config, OtelOptions(service_name="my-service"))
        assert "runtime" in config.client_connect_config

    def test_service_name_from_env(self) -> None:
        config = LambdaWorkerConfig()
        with patch.dict("os.environ", {"OTEL_SERVICE_NAME": "env-service"}):
            apply_defaults(config)
        assert "runtime" in config.client_connect_config

    def test_service_name_from_lambda_function_name(self) -> None:
        config = LambdaWorkerConfig()
        with patch.dict(
            "os.environ",
            {"AWS_LAMBDA_FUNCTION_NAME": "my-lambda"},
            clear=True,
        ):
            apply_defaults(config)
        assert "runtime" in config.client_connect_config

    def test_endpoint_from_env(self) -> None:
        config = LambdaWorkerConfig()
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://custom:4317"},
        ):
            apply_defaults(config)
        assert "runtime" in config.client_connect_config

    def test_default_options_used_when_none(self) -> None:
        config = LambdaWorkerConfig()
        apply_defaults(config)
        assert "runtime" in config.client_connect_config
        assert len(config.shutdown_hooks) == 1


class TestOtelOptions:
    def test_defaults(self) -> None:
        opts = OtelOptions()
        assert opts.service_name == ""
        assert opts.collector_endpoint == ""
        assert opts.metric_periodicity == timedelta(seconds=10)

    def test_custom_values(self) -> None:
        opts = OtelOptions(
            service_name="svc",
            collector_endpoint="http://host:4317",
            metric_periodicity=timedelta(seconds=30),
        )
        assert opts.service_name == "svc"
        assert opts.collector_endpoint == "http://host:4317"
        assert opts.metric_periodicity == timedelta(seconds=30)
