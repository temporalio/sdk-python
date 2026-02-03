from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import opentelemetry.sdk.trace
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import TracerProvider, set_tracer_provider

from temporalio.contrib.opentelemetryv2 import TracingInterceptor
from temporalio.contrib.opentelemetryv2._id_generator import TemporalIdGenerator
from temporalio.contrib.opentelemetryv2._processor import TemporalSpanProcessor
from temporalio.plugin import SimplePlugin


class OpenTelemetryPlugin(SimplePlugin):
    """OpenTelemetry v2 plugin for Temporal SDK.

    This plugin integrates OpenTelemetry tracing with the Temporal SDK, providing
    automatic span creation for workflows, activities, and other Temporal operations.
    """

    def __init__(
        self, exporters: Sequence[SpanExporter], *, add_temporal_spans: bool = False
    ):
        """Initialize the OpenTelemetry plugin.

        Args:
            exporters: Sequence of OpenTelemetry span exporters to use.
            add_temporal_spans: Whether to add additional Temporal-specific spans
                for operations like StartWorkflow, RunWorkflow, etc.
        """
        generator = TemporalIdGenerator()
        self._provider = opentelemetry.sdk.trace.TracerProvider(id_generator=generator)
        for exporter in exporters:
            self._provider.add_span_processor(TemporalSpanProcessor(exporter))

        interceptors = [
            TracingInterceptor(self._provider.get_tracer(__name__), add_temporal_spans)
        ]

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            set_tracer_provider(self._provider)
            yield

        super().__init__(
            "OpenTelemetryPlugin",
            client_interceptors=interceptors,
            worker_interceptors=interceptors,
            run_context=lambda: run_context(),
        )

    def provider(self) -> TracerProvider:
        """Get the OpenTelemetry TracerProvider instance.

        Returns:
            The TracerProvider used by this plugin.
        """
        return self._provider
