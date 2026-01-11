"""OpenTelemetry tracing plugin for Temporal workflows."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner

from ._replay_filtering_processor import ReplayFilteringSpanProcessor
from ._tracing_interceptor import TracingInterceptor

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

    from temporalio.worker.workflow_sandbox import SandboxRestrictions


class OtelTracingPlugin(SimplePlugin):
    """Plugin for clean OTEL tracing in Temporal workflows.

    This plugin provides:
    1. Context propagation via TracingInterceptor (with create_spans=False)
    2. Automatic sandbox passthrough configuration for opentelemetry module
    3. Optional replay filtering for span processors

    The plugin uses TracingInterceptor with create_spans=False, which means
    it propagates trace context through Temporal headers without creating
    its own spans. This allows you to use your own instrumentation
    (like OpenInference) while still getting proper context propagation.

    Usage:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from temporalio.contrib.opentelemetry import OtelTracingPlugin

        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter())
        )

        plugin = OtelTracingPlugin(tracer_provider=tracer_provider)

        client = await Client.connect(
            "localhost:7233",
            plugins=[plugin],
        )

        # Sandbox passthrough is configured automatically by the plugin
        worker = Worker(
            client,
            task_queue="my-queue",
            workflows=[MyWorkflow],
        )
    """

    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        filter_replay_spans: bool = True,
    ) -> None:
        """Initialize the OTEL tracing plugin.

        Args:
            tracer_provider: Optional tracer provider to wrap with replay
                filtering. If provided and filter_replay_spans is True,
                existing span processors will be wrapped with
                ReplayFilteringSpanProcessor.
            filter_replay_spans: If True and tracer_provider is provided,
                wrap span processors to filter out spans created during replay.
                Defaults to True.
        """
        if tracer_provider and filter_replay_spans:
            self._wrap_with_replay_filter(tracer_provider)

        interceptor = TracingInterceptor(create_spans=False)

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner | None:
            """Auto-configure sandbox to passthrough opentelemetry."""
            from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

            if runner is None:
                return None
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "opentelemetry"
                    ),
                )
            return runner

        super().__init__(
            name="OtelTracingPlugin",
            worker_interceptors=[interceptor],
            client_interceptors=[interceptor],
            workflow_runner=workflow_runner,
        )

    @property
    def sandbox_restrictions(self) -> SandboxRestrictions:
        """Return sandbox restrictions with opentelemetry passthrough.

        This property returns a SandboxRestrictions object that has opentelemetry
        added to the passthrough modules. This is necessary for OTEL context
        propagation to work correctly inside workflow sandboxes.

        Without this, the opentelemetry module is re-imported inside the sandbox,
        creating a separate ContextVar instance that cannot see context attached
        by the TracingInterceptor.

        Usage:
            plugin = OtelTracingPlugin()
            worker = Worker(
                client,
                workflows=[...],
                workflow_runner=SandboxedWorkflowRunner(
                    restrictions=plugin.sandbox_restrictions
                ),
            )
        """
        from temporalio.worker.workflow_sandbox import SandboxRestrictions

        return SandboxRestrictions.default.with_passthrough_modules("opentelemetry")

    def _wrap_with_replay_filter(self, tracer_provider: TracerProvider) -> None:
        """Wrap tracer provider's span processors with replay filtering.

        This modifies the tracer provider in place to wrap each span processor
        with ReplayFilteringSpanProcessor.
        """
        # Access the internal span processors
        # Note: This uses internal APIs which may change
        if hasattr(tracer_provider, "_active_span_processor"):
            processor = tracer_provider._active_span_processor
            # The multi span processor has a list of processors
            if hasattr(processor, "_span_processors"):
                wrapped = []
                for p in processor._span_processors:
                    wrapped.append(ReplayFilteringSpanProcessor(p))
                processor._span_processors = tuple(wrapped)
