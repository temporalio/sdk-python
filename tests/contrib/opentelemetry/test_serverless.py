"""Tests for temporalio.contrib.opentelemetry._serverless."""

from __future__ import annotations

import sys
from datetime import timedelta

import pytest
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from temporalio.contrib.opentelemetry import _serverless
from temporalio.runtime import OpenTelemetryConfig


class TestResolveEndpoint:
    def test_explicit_wins(self) -> None:
        assert (
            _serverless.resolve_endpoint(
                "http://explicit:4317",
                env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://env:4317"},
            )
            == "http://explicit:4317"
        )

    def test_env_fallback(self) -> None:
        assert (
            _serverless.resolve_endpoint(
                None, env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://env:4317"}
            )
            == "http://env:4317"
        )

    def test_default_when_nothing_set(self) -> None:
        assert _serverless.resolve_endpoint(None, env={}) == (
            _serverless.DEFAULT_OTLP_ENDPOINT
        )

    def test_empty_explicit_and_env_are_skipped(self) -> None:
        assert (
            _serverless.resolve_endpoint("", env={"OTEL_EXPORTER_OTLP_ENDPOINT": ""})
            == _serverless.DEFAULT_OTLP_ENDPOINT
        )

    def test_custom_default(self) -> None:
        assert (
            _serverless.resolve_endpoint(None, env={}, default="http://custom:4317")
            == "http://custom:4317"
        )


class TestResolveServiceName:
    def test_explicit_wins(self) -> None:
        assert (
            _serverless.resolve_service_name(
                "explicit",
                ["FALLBACK"],
                "default",
                env={"OTEL_SERVICE_NAME": "otel", "FALLBACK": "fallback"},
            )
            == "explicit"
        )

    def test_otel_service_name_precedes_fallbacks(self) -> None:
        assert (
            _serverless.resolve_service_name(
                None,
                ["FALLBACK"],
                "default",
                env={"OTEL_SERVICE_NAME": "otel", "FALLBACK": "fallback"},
            )
            == "otel"
        )

    def test_fallback_env_vars_checked_in_order(self) -> None:
        assert (
            _serverless.resolve_service_name(
                None,
                ["FIRST", "SECOND"],
                "default",
                env={"SECOND": "second-value"},
            )
            == "second-value"
        )
        assert (
            _serverless.resolve_service_name(
                None,
                ["FIRST", "SECOND"],
                "default",
                env={"FIRST": "first-value", "SECOND": "second-value"},
            )
            == "first-value"
        )

    def test_default_when_nothing_set(self) -> None:
        assert (
            _serverless.resolve_service_name(None, ["FALLBACK"], "default", env={})
            == "default"
        )

    def test_empty_values_are_skipped(self) -> None:
        # Empty strings are falsy and are skipped, falling through to the next
        # source in the chain.
        assert (
            _serverless.resolve_service_name(
                "",
                ["FIRST", "SECOND"],
                "default",
                env={"OTEL_SERVICE_NAME": "", "FIRST": "", "SECOND": "second-value"},
            )
            == "second-value"
        )

    def test_whitespace_is_not_stripped(self) -> None:
        # No stripping: a whitespace-only value is truthy and is used as-is,
        # matching the pre-existing AWS Lambda behavior.
        assert (
            _serverless.resolve_service_name(
                None,
                ["FALLBACK"],
                "default",
                env={"OTEL_SERVICE_NAME": " ", "FALLBACK": "fallback"},
            )
            == " "
        )


class TestBuildMetricsTelemetryConfig:
    def test_with_service_name(self) -> None:
        config = _serverless.build_metrics_telemetry_config(
            endpoint="http://collector:4317",
            service_name="my-service",
            metric_periodicity=timedelta(seconds=30),
        )
        assert isinstance(config.metrics, OpenTelemetryConfig)
        assert config.metrics.url == "http://collector:4317"
        assert config.metrics.metric_periodicity == timedelta(seconds=30)
        assert config.global_tags == {"service_name": "my-service"}

    def test_without_service_name(self) -> None:
        config = _serverless.build_metrics_telemetry_config(
            endpoint="http://collector:4317",
            service_name="",
            metric_periodicity=None,
        )
        assert isinstance(config.metrics, OpenTelemetryConfig)
        assert config.metrics.url == "http://collector:4317"
        assert config.metrics.metric_periodicity is None
        assert config.global_tags == {}

    def test_empty_endpoint_uses_default(self) -> None:
        config = _serverless.build_metrics_telemetry_config(
            endpoint="",
            service_name="svc",
            metric_periodicity=None,
        )
        assert isinstance(config.metrics, OpenTelemetryConfig)
        assert config.metrics.url == _serverless.DEFAULT_OTLP_ENDPOINT


class TestBuildOtlpSpanProcessor:
    def test_returns_batch_span_processor(self) -> None:
        processor = _serverless.build_otlp_span_processor("http://localhost:4317")
        try:
            assert isinstance(processor, BatchSpanProcessor)
        finally:
            processor.shutdown()

    def test_raises_import_error_when_exporter_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Setting the module to None in sys.modules makes the lazy import raise
        # ImportError, simulating the exporter not being installed.
        monkeypatch.setitem(
            sys.modules,
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
            None,
        )
        with pytest.raises(ImportError):
            _serverless.build_otlp_span_processor("http://localhost:4317")
