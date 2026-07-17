"""Google Cloud integrations for Temporal SDK.

.. warning::
    This package is experimental and may change in future versions.
    Use with caution in production environments.

The OpenTelemetry integration is designed for container-based Cloud Run
workers, especially worker pools, and exports telemetry to a collector
sidecar by default.
"""

from temporalio.contrib.gcp._opentelemetry import (
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
