from opentelemetry.trace import get_tracer_provider

from temporalio.contrib.opentelemetry import OpenTelemetryInterceptor
from temporalio.contrib.opentelemetry._tracer_provider import ReplaySafeTracerProvider
from temporalio.plugin import SimplePlugin


class OpenTelemetryPlugin(SimplePlugin):
    """OpenTelemetry plugin for Temporal SDK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin integrates OpenTelemetry tracing with the Temporal SDK, providing
    automatic span creation for workflows, activities, and other Temporal operations.
    It uses the new OpenTelemetryInterceptor implementation.

    Unlike the prior TracingInterceptor, this allows for accurate duration spans and parenting inside a workflow
    with temporalio.contrib.opentelemetry.workflow.tracer()

    When starting traces on the client side, you can use OpenTelemetryPlugin.provider() to trace to the same
    exporters provided. If you don't, ensure that some provider is globally registered or the client side
    traces will not be propagated to the workflow.
    """

    def __init__(self, *, add_temporal_spans: bool = False):
        """Initialize the OpenTelemetry plugin.

        Args:
            add_temporal_spans: Whether to add additional Temporal-specific spans
                for operations like StartWorkflow, RunWorkflow, etc.
        """
        provider = get_tracer_provider()
        if not isinstance(provider, ReplaySafeTracerProvider):
            raise ValueError(
                "When using OpenTelemetryPlugin, the global trace provider must be a ReplaySafeTracerProvider. Use init_tracer_provider to create one."
            )

        interceptors = [
            OpenTelemetryInterceptor(provider.get_tracer(__name__), add_temporal_spans)
        ]

        super().__init__(
            "OpenTelemetryPlugin",
            client_interceptors=interceptors,
            worker_interceptors=interceptors,
        )
