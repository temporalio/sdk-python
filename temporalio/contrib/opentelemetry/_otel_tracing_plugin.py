"""OpenTelemetry tracing plugin for Temporal workflows."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner

from ._replay_filtering_processor import ReplayFilteringSpanProcessor
from ._tracing_interceptor import TracingInterceptor

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

    from temporalio.worker.workflow_sandbox import SandboxRestrictions

_logger = logging.getLogger(__name__)


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

    Why create_spans=False?

    OpenTelemetry spans cannot cross process boundaries - only SpanContext
    can be propagated. Temporal workflows may execute across multiple workers
    (different processes/machines), so we propagate context only and let
    your instrumentation (e.g., OpenInference) create spans locally.

    Trace continuity is maintained via parent-child relationships:

    - Client creates a span, its SpanContext is propagated via headers
    - Worker receives SpanContext, wraps it in NonRecordingSpan
    - Your instrumentation creates child spans with the correct parent
    - Backend correlates spans by trace_id and parent span_id

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
        deterministic_ids: bool = False,
    ) -> None:
        """Initialize the OTEL tracing plugin.

        Args:
            tracer_provider: Optional tracer provider to configure. If provided,
                replay filtering and/or deterministic IDs will be configured
                based on the other parameters.
            filter_replay_spans: If True and tracer_provider is provided,
                wrap span processors to filter out spans created during replay.
                Defaults to True.
            deterministic_ids: If True and tracer_provider is provided,
                configure the tracer provider to use deterministic span ID
                generation in workflow context. This enables real-duration
                spans in workflows by ensuring the same span IDs are generated
                on replay (which are then filtered by ReplayFilteringSpanProcessor).
                Defaults to False.
        """
        if tracer_provider:
            if deterministic_ids:
                self._configure_deterministic_ids(tracer_provider)
            if filter_replay_spans:
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

    def _configure_deterministic_ids(self, tracer_provider: TracerProvider) -> None:
        """Configure tracer provider for deterministic span ID generation.

        This modifies the tracer provider in place to use TemporalIdGenerator,
        which produces deterministic span/trace IDs when running in workflow
        context using workflow.random().

        Args:
            tracer_provider: The tracer provider to configure.
        """
        from ._id_generator import TemporalIdGenerator

        if hasattr(tracer_provider, "id_generator"):
            tracer_provider.id_generator = TemporalIdGenerator()
        else:
            _logger.warning(
                "Could not configure deterministic span IDs: "
                "TracerProvider does not have id_generator attribute. "
                "Span IDs will be random, which may cause issues during replay."
            )
