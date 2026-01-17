"""Tests for deterministic span ID generation in Temporal workflows."""

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import opentelemetry.trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.opentelemetry import OtelTracingPlugin, TemporalIdGenerator
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


class TestTemporalIdGeneratorUnit:
    """Unit tests for TemporalIdGenerator."""

    def test_fallback_to_random_outside_workflow(self):
        """Verify TemporalIdGenerator uses random IDs outside workflow context."""
        id_gen = TemporalIdGenerator()

        # Outside workflow context, should use fallback random generator
        span_id_1 = id_gen.generate_span_id()
        span_id_2 = id_gen.generate_span_id()

        # IDs should be valid (non-zero)
        assert span_id_1 != 0
        assert span_id_2 != 0
        # IDs should be different (random)
        assert span_id_1 != span_id_2

    def test_fallback_trace_id_outside_workflow(self):
        """Verify TemporalIdGenerator uses random trace IDs outside workflow context."""
        id_gen = TemporalIdGenerator()

        trace_id_1 = id_gen.generate_trace_id()
        trace_id_2 = id_gen.generate_trace_id()

        # IDs should be valid (non-zero)
        assert trace_id_1 != 0
        assert trace_id_2 != 0
        # IDs should be different (random)
        assert trace_id_1 != trace_id_2

    def test_deterministic_span_id_in_workflow_context(self):
        """Verify TemporalIdGenerator uses workflow.random() in workflow context."""
        id_gen = TemporalIdGenerator()

        # Mock workflow.in_workflow() and workflow.random()
        mock_random = MagicMock()
        mock_random.getrandbits.side_effect = [12345, 67890]

        with patch("temporalio.workflow.in_workflow", return_value=True):
            with patch("temporalio.workflow.random", return_value=mock_random):
                span_id_1 = id_gen.generate_span_id()
                span_id_2 = id_gen.generate_span_id()

        assert span_id_1 == 12345
        assert span_id_2 == 67890

    def test_deterministic_trace_id_in_workflow_context(self):
        """Verify TemporalIdGenerator uses workflow.random() for trace IDs in workflow."""
        id_gen = TemporalIdGenerator()

        mock_random = MagicMock()
        mock_random.getrandbits.side_effect = [
            123456789012345678901234567890,
            987654321098765432109876543210,
        ]

        with patch("temporalio.workflow.in_workflow", return_value=True):
            with patch("temporalio.workflow.random", return_value=mock_random):
                trace_id_1 = id_gen.generate_trace_id()
                trace_id_2 = id_gen.generate_trace_id()

        assert trace_id_1 == 123456789012345678901234567890
        assert trace_id_2 == 987654321098765432109876543210


class TestOtelTracingPluginDeterministicIds:
    """Tests for OtelTracingPlugin with deterministic_ids parameter."""

    def test_deterministic_ids_false_by_default(self):
        """Verify deterministic_ids is False by default (backward compat)."""
        provider = TracerProvider()
        original_id_gen = provider.id_generator

        # Create plugin without deterministic_ids
        OtelTracingPlugin(tracer_provider=provider)

        # id_generator should be unchanged
        assert provider.id_generator is original_id_gen

    def test_deterministic_ids_true_configures_generator(self):
        """Verify deterministic_ids=True configures TemporalIdGenerator."""
        provider = TracerProvider()

        OtelTracingPlugin(tracer_provider=provider, deterministic_ids=True)

        assert isinstance(provider.id_generator, TemporalIdGenerator)

    def test_deterministic_ids_without_tracer_provider(self):
        """Verify deterministic_ids has no effect without tracer_provider."""
        # Should not raise - just creates plugin without configuring anything
        plugin = OtelTracingPlugin(deterministic_ids=True)
        assert plugin is not None


# =============================================================================
# Integration Tests
# =============================================================================


@activity.defn
async def record_span_id_activity() -> dict[str, Any]:
    """Activity that creates a span and records its ID."""
    tracer = opentelemetry.trace.get_tracer(__name__)
    with tracer.start_as_current_span("activity_span") as span:
        span_context = span.get_span_context()
        return {
            "trace_id": span_context.trace_id,
            "span_id": span_context.span_id,
        }


@workflow.defn
class SpanIdTestWorkflow:
    """Workflow that creates spans and records their IDs."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        tracer = opentelemetry.trace.get_tracer(__name__)

        # Create a workflow span
        with tracer.start_as_current_span("workflow_span") as span:
            span_context = span.get_span_context()
            workflow_span_id = span_context.span_id
            workflow_trace_id = span_context.trace_id

        # Call activity to get activity span ID
        activity_result = await workflow.execute_activity(
            record_span_id_activity,
            start_to_close_timeout=timedelta(seconds=30),
        )

        return {
            "workflow_span_id": workflow_span_id,
            "workflow_trace_id": workflow_trace_id,
            "activity_span_id": activity_result["span_id"],
            "activity_trace_id": activity_result["trace_id"],
        }


@pytest.fixture
def tracer_provider_with_exporter():
    """Create a TracerProvider with InMemorySpanExporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.mark.asyncio
async def test_deterministic_span_ids_in_workflow(tracer_provider_with_exporter):
    """Integration test: verify span IDs are deterministic in workflow context."""
    provider, exporter = tracer_provider_with_exporter

    # Set as global tracer provider
    opentelemetry.trace.set_tracer_provider(provider)

    plugin = OtelTracingPlugin(
        tracer_provider=provider,
        deterministic_ids=True,
        filter_replay_spans=True,
    )

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"test-queue-{uuid.uuid4()}"

        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            plugins=[plugin],
        )

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SpanIdTestWorkflow],
            activities=[record_span_id_activity],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=plugin.sandbox_restrictions
            ),
        ):
            result = await client.execute_workflow(
                SpanIdTestWorkflow.run,
                id=f"test-workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

        # Verify we got span IDs
        assert result["workflow_span_id"] != 0, "Workflow span ID should be non-zero"
        assert result["activity_span_id"] != 0, "Activity span ID should be non-zero"

        # Check exported spans
        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert "workflow_span" in span_names, "Workflow span should be exported"
        assert "activity_span" in span_names, "Activity span should be exported"


