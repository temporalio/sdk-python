from opentelemetry.trace import get_tracer_provider

from temporalio.contrib.opentelemetry import OpenTelemetryInterceptor
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

    Your tracer provider should be created with `create_tracer_provider` for it to be used within a Temporal worker.
    """

    def __init__(self, *, add_temporal_spans: bool = False):
        """Initialize the OpenTelemetry plugin.

        Args:
            add_temporal_spans: Whether to add additional Temporal-specific spans
                for operations like StartWorkflow, RunWorkflow, etc.
        """
        provider = get_tracer_provider()

        interceptors = [OpenTelemetryInterceptor(provider, add_temporal_spans)]

        super().__init__(
            "OpenTelemetryPlugin",
            client_interceptors=interceptors,
            worker_interceptors=interceptors,
        )
