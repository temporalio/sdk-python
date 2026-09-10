"""OpenTelemetry integration for Temporal workers running on Google Cloud Run.

This package is designed for container-based Cloud Run workers, especially
worker pools, and exports telemetry to an OpenTelemetry collector sidecar by
default.

.. warning::
    This package is experimental and may change in future versions.
    Use with caution in production environments.
"""

from temporalio.contrib.gcp.cloud_run.opentelemetry._opentelemetry import (
    CLOUD_RUN_SERVICE_ENV_VAR,
    CLOUD_RUN_WORKER_POOL_ENV_VAR,
    DEFAULT_FLUSH_TIMEOUT,
    DEFAULT_METRIC_PERIODICITY,
    DEFAULT_OTLP_ENDPOINT,
    DEFAULT_SERVICE_NAME,
    OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR,
    OTEL_SERVICE_NAME_ENV_VAR,
    OpenTelemetryPlugin,
    build_metrics_telemetry_config,
)

__all__ = [
    "CLOUD_RUN_SERVICE_ENV_VAR",
    "CLOUD_RUN_WORKER_POOL_ENV_VAR",
    "DEFAULT_FLUSH_TIMEOUT",
    "DEFAULT_METRIC_PERIODICITY",
    "DEFAULT_OTLP_ENDPOINT",
    "DEFAULT_SERVICE_NAME",
    "OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR",
    "OTEL_SERVICE_NAME_ENV_VAR",
    "OpenTelemetryPlugin",
    "build_metrics_telemetry_config",
]
