"""Provider-neutral OpenTelemetry helpers shared by serverless integrations.

This is a private module. It is intentionally **not** re-exported from
``temporalio.contrib.opentelemetry`` so that importing that package gains no new
imports. The OTLP gRPC span exporter is imported lazily inside
:py:func:`build_otlp_span_processor`, so importing this module does not require
the ``opentelemetry-exporter-otlp-proto-grpc`` dependency.

The AWS Lambda and GCP Cloud Run integrations layer their provider-specific
policy (ID generators, default service names, environment fallbacks) on top of
these helpers. Empty environment/argument values are skipped with a plain
truthiness check and are not stripped of whitespace, matching the pre-existing
AWS Lambda resolution behavior.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import timedelta

from opentelemetry.sdk.trace.export import BatchSpanProcessor

from temporalio.runtime import OpenTelemetryConfig, TelemetryConfig

OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
"""Standard OpenTelemetry environment variable for the common OTLP endpoint."""

OTEL_SERVICE_NAME_ENV_VAR = "OTEL_SERVICE_NAME"
"""Standard OpenTelemetry environment variable for ``service.name``."""

DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
"""Default local OTLP gRPC collector endpoint."""


def resolve_endpoint(
    explicit: str | None,
    *,
    env: Mapping[str, str] = os.environ,
    default: str = DEFAULT_OTLP_ENDPOINT,
) -> str:
    """Resolve the OTLP collector endpoint.

    Resolution order: ``explicit`` -> ``OTEL_EXPORTER_OTLP_ENDPOINT`` ->
    ``default``. Empty values are skipped with a truthiness check, without
    stripping whitespace.

    Args:
        explicit: Endpoint supplied directly by the caller, if any.
        env: Environment mapping. Defaults to the live process environment.
        default: Endpoint used when nothing else is provided.

    Returns:
        The resolved endpoint.
    """
    return explicit or env.get(OTEL_EXPORTER_OTLP_ENDPOINT_ENV_VAR) or default


def resolve_service_name(
    explicit: str | None,
    fallback_env_vars: Sequence[str],
    default: str,
    *,
    env: Mapping[str, str] = os.environ,
) -> str:
    """Resolve the OpenTelemetry service name.

    Resolution order: ``explicit`` -> ``OTEL_SERVICE_NAME`` -> each name in
    ``fallback_env_vars`` in order -> ``default``. Empty values are skipped with
    a truthiness check, without stripping whitespace.

    Args:
        explicit: Service name supplied directly by the caller, if any.
        fallback_env_vars: Provider-specific environment variable names checked,
            in order, after ``OTEL_SERVICE_NAME``.
        default: Service name used when nothing else is provided.
        env: Environment mapping. Defaults to the live process environment.

    Returns:
        The resolved service name.
    """
    if explicit:
        return explicit
    otel_service_name = env.get(OTEL_SERVICE_NAME_ENV_VAR)
    if otel_service_name:
        return otel_service_name
    for name in fallback_env_vars:
        value = env.get(name)
        if value:
            return value
    return default


def build_metrics_telemetry_config(
    *,
    endpoint: str,
    service_name: str,
    metric_periodicity: timedelta | None,
) -> TelemetryConfig:
    """Build Core telemetry configuration for OTLP metrics export.

    Args:
        endpoint: OTLP collector endpoint. Falls back to
            :py:data:`DEFAULT_OTLP_ENDPOINT` when empty.
        service_name: Service name added as the ``service_name`` global tag.
            When empty, no global tag is added.
        metric_periodicity: Metric export interval, passed through unchanged.

    Returns:
        A :py:class:`temporalio.runtime.TelemetryConfig` with metrics pointed at
        the collector.
    """
    return TelemetryConfig(
        metrics=OpenTelemetryConfig(
            url=endpoint or DEFAULT_OTLP_ENDPOINT,
            metric_periodicity=metric_periodicity,
        ),
        global_tags={"service_name": service_name} if service_name else {},
    )


def build_otlp_span_processor(
    endpoint: str,
    *,
    insecure: bool = True,
) -> BatchSpanProcessor:
    """Build a batch span processor backed by the OTLP gRPC exporter.

    The exporter is imported lazily so that importing this module does not
    require ``opentelemetry-exporter-otlp-proto-grpc``.

    Args:
        endpoint: OTLP collector endpoint.
        insecure: Whether to use an insecure (non-TLS) gRPC channel.

    Returns:
        A batch span processor that exports to the OTLP collector.

    Raises:
        ImportError: If the OTLP gRPC exporter is not installed. The caller
            decides whether to warn and continue or re-raise.
    """
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )

    return BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
