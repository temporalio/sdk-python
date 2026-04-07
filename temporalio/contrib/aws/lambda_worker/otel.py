"""OpenTelemetry helpers for Temporal workers running inside AWS Lambda.

Use :py:func:`apply_defaults` inside a :py:func:`run_worker` configure callback for a
batteries-included setup that creates an OTel collector exporter and tracing plugin, suitable
for use with the AWS Distro for OpenTelemetry (ADOT) Lambda layer.

Use :py:func:`apply_tracing` or :py:func:`build_metrics_telemetry_config` individually if you only
need one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.attributes.service_attributes import SERVICE_NAME
from opentelemetry.trace import get_tracer_provider, set_tracer_provider

from temporalio.contrib.aws.lambda_worker._configure import LambdaWorkerConfig
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
from temporalio.runtime import OpenTelemetryConfig, Runtime, TelemetryConfig

logger = logging.getLogger(__name__)


@dataclass
class OtelOptions:
    """Options for :py:func:`apply_defaults`.

    Attributes:
        metric_periodicity: How often the Core SDK exports metrics to the
            collector. Defaults to 10 seconds. Set this shorter than your
            Lambda timeout to ensure at least one export per invocation.
        service_name: OTel service name resource attribute. If empty,
            falls back to ``OTEL_SERVICE_NAME``, then
            ``AWS_LAMBDA_FUNCTION_NAME``, then
            ``"temporal-lambda-worker"``.
        collector_endpoint: OTLP collector endpoint (e.g.
            ``"http://localhost:4317"``). If empty, falls back to
            ``OTEL_EXPORTER_OTLP_ENDPOINT``, then
            ``"http://localhost:4317"``.
    """

    metric_periodicity: timedelta = field(default_factory=lambda: timedelta(seconds=10))
    service_name: str = ""
    collector_endpoint: str = ""


def _resolve_service_name(options: OtelOptions) -> str:
    service_name = options.service_name
    if not service_name:
        service_name = os.environ.get("OTEL_SERVICE_NAME", "")
    if not service_name:
        service_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
    if not service_name:
        service_name = "temporal-lambda-worker"
    return service_name


def _resolve_endpoint(options: OtelOptions) -> str:
    endpoint = options.collector_endpoint
    if not endpoint:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        endpoint = "http://localhost:4317"
    return endpoint


def apply_defaults(
    config: LambdaWorkerConfig,
    options: OtelOptions | None = None,
) -> None:
    """Configure OTel metrics and tracing with AWS Lambda defaults.

    Sets up Core SDK metrics export via a :py:class:`temporalio.runtime.Runtime` with an
    :py:class:`temporalio.runtime.OpenTelemetryConfig` pointing at the OTLP collector, and adds the
    :py:class:`temporalio.contrib.opentelemetry.OpenTelemetryPlugin` for distributed tracing with
    workflow sandbox passthrough.

    Creates a replay-safe ``TracerProvider`` (with X-Ray ID generator and OTLP gRPC exporter if
    available) and sets it as the global OpenTelemetry tracer provider. The
    :py:class:`temporalio.contrib.opentelemetry.OpenTelemetryPlugin` uses the global provider, so
    it must be set before the worker starts.

    The collector endpoint defaults to ``http://localhost:4317``, which is the endpoint expected by
    the ADOT collector Lambda layer.

    Registers a per-invocation ``ForceFlush`` shutdown hook for the global ``TracerProvider`` so
    pending traces are exported before each Lambda invocation completes.

    Metrics are exported on the ``metric_periodicity`` interval by the runtime's internal thread.
    There is no explicit flush API for these metrics; set ``metric_periodicity`` short enough to
    ensure at least one export per invocation.

    Args:
        config: The :py:class:`LambdaWorkerConfig` to configure.
        options: Optional overrides for service name, endpoint, etc.
    """
    if options is None:
        options = OtelOptions()

    endpoint = _resolve_endpoint(options)
    service_name = _resolve_service_name(options)

    telemetry_config = build_metrics_telemetry_config(
        endpoint=endpoint,
        service_name=service_name,
        metric_periodicity=options.metric_periodicity,
    )
    runtime = Runtime(telemetry=telemetry_config)
    config.client_connect_config["runtime"] = runtime

    resource = Resource.create({SERVICE_NAME: service_name})

    # Try to use X-Ray ID generator if available.
    try:
        from opentelemetry.sdk.extension.aws.trace import (  # type: ignore[reportMissingTypeStubs]
            AwsXRayIdGenerator,
        )

        tracer_provider = create_tracer_provider(
            resource=resource, id_generator=AwsXRayIdGenerator()
        )
    except ImportError:
        logger.warning(
            "opentelemetry-sdk-extension-aws is not installed; "
            "X-Ray trace ID generation is disabled. "
            "Install the 'lambda-worker-otel' extra for full ADOT support."
        )
        tracer_provider = create_tracer_provider(resource=resource)

    # Use OTLP gRPC exporter if available, otherwise skip trace export.
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
    except ImportError:
        logger.warning(
            "opentelemetry-exporter-otlp-proto-grpc is not installed; "
            "traces will not be exported to the OTLP collector. "
            "Install the 'lambda-worker-otel' extra for full ADOT support."
        )

    # Set as global so the OpenTelemetryPlugin picks it up.
    set_tracer_provider(tracer_provider)

    apply_tracing(config)


def build_metrics_telemetry_config(
    *,
    endpoint: str = "",
    service_name: str = "",
    metric_periodicity: timedelta | None = None,
) -> TelemetryConfig:
    """Build a :py:class:`temporalio.runtime.TelemetryConfig` for OTel metrics.

    Returns a ``TelemetryConfig`` with :py:class:`temporalio.runtime.OpenTelemetryConfig` metrics
    pointed at the given OTLP collector endpoint. Use this when you need to compose metrics config
    with other telemetry settings (e.g. custom logging) into your own
    :py:class:`temporalio.runtime.Runtime`.

    Core SDK metrics are exported on the ``metric_periodicity`` interval by the runtime's internal
    thread. There is no explicit flush API; set ``metric_periodicity`` short enough to ensure at
    least one export per Lambda invocation.

    Example::

        telemetry = build_metrics_telemetry_config(
            endpoint="http://localhost:4317",
            service_name="my-service",
        )
        # Customize further:
        telemetry_config = dataclasses.replace(
            telemetry, logging=my_logging_config
        )
        runtime = Runtime(telemetry=telemetry_config)
        config.client_connect_config["runtime"] = runtime

    Args:
        endpoint: OTLP collector endpoint. Defaults to
            ``http://localhost:4317``.
        service_name: OTel service name. Used as a global tag.
        metric_periodicity: How often metrics are exported.

    Returns:
        A ``TelemetryConfig`` ready to pass to
        :py:class:`temporalio.runtime.Runtime`.
    """
    if not endpoint:
        endpoint = "http://localhost:4317"

    otel_config = OpenTelemetryConfig(
        url=endpoint,
        metric_periodicity=metric_periodicity,
    )

    global_tags: dict[str, str] = {}
    if service_name:
        global_tags["service_name"] = service_name

    return TelemetryConfig(
        metrics=otel_config,
        global_tags=global_tags,
    )


def apply_tracing(config: LambdaWorkerConfig) -> None:
    """Configure only OTel tracing (no metrics) on the Lambda worker config.

    Adds an :py:class:`temporalio.contrib.opentelemetry.OpenTelemetryPlugin` to
    ``config.worker_config["plugins"]``. The plugin uses the global
    ``TracerProvider`` set via ``opentelemetry.trace.set_tracer_provider``.
    Ensure your provider is set globally before the worker starts.

    Also registers a ``ForceFlush`` shutdown hook that flushes the global
    ``TracerProvider`` (if it supports ``force_flush``).

    Args:
        config: The :py:class:`LambdaWorkerConfig` to configure.
    """
    plugin = OpenTelemetryPlugin()
    plugins = list(config.worker_config.get("plugins", []))
    plugins.append(plugin)
    config.worker_config["plugins"] = plugins

    async def _flush() -> None:
        provider = get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if flush is not None:
            flush()

    config.shutdown_hooks.append(_flush)
