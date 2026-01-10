"""Tests for OpenTelemetry sandbox passthrough behavior.

These tests verify that OTEL context propagates correctly when opentelemetry
is configured as a passthrough module in the sandbox.
"""

import uuid
from typing import Any

import pytest

import opentelemetry.trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from temporalio.contrib.opentelemetry import TracingInterceptor, OtelTracingPlugin


@workflow.defn
class SandboxSpanTestWorkflow:
    """Workflow that tests get_current_span() inside the sandbox."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        # This tests that get_current_span() returns the propagated span
        # INSIDE the sandbox. Without passthrough, this returns INVALID_SPAN.
        span = opentelemetry.trace.get_current_span()
        span_context = span.get_span_context()

        # Also check if context was propagated via headers
        headers = workflow.info().headers
        has_tracer_header = "_tracer-data" in headers

        return {
            "is_valid": span is not opentelemetry.trace.INVALID_SPAN,
            "trace_id": span_context.trace_id if span_context else 0,
            "span_id": span_context.span_id if span_context else 0,
            "has_tracer_header": has_tracer_header,
            "span_str": str(span),
        }


@pytest.fixture
def tracer_provider_and_exporter():
    """Create a tracer provider with in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)
    return provider, exporter


@pytest.mark.asyncio
async def test_sandbox_context_propagation_without_passthrough(
    tracer_provider_and_exporter,
):
    """Test that context does NOT propagate without passthrough.

    This test verifies the problem: without opentelemetry passthrough,
    get_current_span() returns INVALID_SPAN inside the sandbox.
    """
    provider, exporter = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    interceptor = TracingInterceptor()

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"test-queue-{uuid.uuid4()}"

        # Create a client with the tracing interceptor
        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            interceptors=[interceptor],
        )

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxSpanTestWorkflow],
            interceptors=[interceptor],
            # Default sandboxed runner - NO passthrough
        ):
            with tracer.start_as_current_span("client_root") as root:
                result = await client.execute_workflow(
                    SandboxSpanTestWorkflow.run,
                    id=f"test-workflow-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

        # Without passthrough, context is propagated via headers but
        # get_current_span() still returns INVALID_SPAN because the sandbox
        # re-imports opentelemetry creating a separate ContextVar
        assert result["has_tracer_header"], "Tracer header should be present"
        assert not result["is_valid"], "Without passthrough, span should be INVALID_SPAN"


@pytest.mark.asyncio
async def test_sandbox_context_propagation_with_passthrough(
    tracer_provider_and_exporter,
):
    """Test that context DOES propagate with passthrough.

    This test verifies the fix: with opentelemetry passthrough,
    get_current_span() returns the propagated span inside the sandbox.
    """
    provider, exporter = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    interceptor = TracingInterceptor()

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"test-queue-{uuid.uuid4()}"

        # Create a client with the tracing interceptor
        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            interceptors=[interceptor],
        )

        # Use SandboxedWorkflowRunner with opentelemetry passthrough
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxSpanTestWorkflow],
            interceptors=[interceptor],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "opentelemetry"
                )
            ),
        ):
            with tracer.start_as_current_span("client_root") as root:
                root_context = root.get_span_context()

                result = await client.execute_workflow(
                    SandboxSpanTestWorkflow.run,
                    id=f"test-workflow-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

        # With passthrough, span should be valid and have the same trace_id
        print(f"Result: {result}")
        print(f"Root trace_id: {root_context.trace_id}")
        assert result["has_tracer_header"], "Tracer header should be present"
        assert result["is_valid"], f"With passthrough, span should be valid. Got: {result['span_str']}"
        assert result["trace_id"] == root_context.trace_id, "Trace ID should match"


@pytest.mark.asyncio
async def test_otel_tracing_plugin_provides_sandbox_restrictions(
    tracer_provider_and_exporter,
):
    """Test that OtelTracingPlugin provides correct sandbox restrictions."""
    provider, _ = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    plugin = OtelTracingPlugin(tracer_provider=provider)

    # Verify the plugin provides sandbox_restrictions property
    restrictions = plugin.sandbox_restrictions
    assert "opentelemetry" in restrictions.passthrough_modules

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
            workflows=[SandboxSpanTestWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=plugin.sandbox_restrictions
            ),
        ):
            with tracer.start_as_current_span("client_root") as root:
                root_context = root.get_span_context()

                result = await client.execute_workflow(
                    SandboxSpanTestWorkflow.run,
                    id=f"test-workflow-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

        # With plugin's sandbox_restrictions, context should propagate
        assert result["is_valid"], "With plugin restrictions, span should be valid"
        assert result["trace_id"] == root_context.trace_id, "Trace ID should match"


@pytest.mark.asyncio
async def test_no_state_leakage_between_workflows(
    tracer_provider_and_exporter,
):
    """Test that context doesn't leak between sequential workflow runs."""
    provider, exporter = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    interceptor = TracingInterceptor()

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"test-queue-{uuid.uuid4()}"

        # Create a client with the tracing interceptor
        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            interceptors=[interceptor],
        )

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxSpanTestWorkflow],
            interceptors=[interceptor],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "opentelemetry"
                )
            ),
        ):
            # Run first workflow with one trace
            with tracer.start_as_current_span("trace_1") as span_1:
                trace_1_id = span_1.get_span_context().trace_id
                result_1 = await client.execute_workflow(
                    SandboxSpanTestWorkflow.run,
                    id=f"test-workflow-1-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

            # Run second workflow with different trace
            with tracer.start_as_current_span("trace_2") as span_2:
                trace_2_id = span_2.get_span_context().trace_id
                result_2 = await client.execute_workflow(
                    SandboxSpanTestWorkflow.run,
                    id=f"test-workflow-2-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

        # Each workflow should see its own trace context
        assert result_1["is_valid"]
        assert result_2["is_valid"]
        assert result_1["trace_id"] == trace_1_id
        assert result_2["trace_id"] == trace_2_id
        assert result_1["trace_id"] != result_2["trace_id"], "Traces should be independent"
