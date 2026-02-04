"""Tests for workflow_span helper during replay.

This test verifies that workflow_span correctly prevents duplicate spans
when workflows replay (with max_cached_workflows=0).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents import workflow_span
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

# OTEL tracer for activities
tracer = trace.get_tracer(__name__)


@activity.defn
async def research_activity(query: str) -> str:
    """Simulates a research activity that creates OTEL spans."""
    with tracer.start_as_current_span("research_work") as span:
        span.set_attribute("query", query)
        await asyncio.sleep(0.01)
        return f"Research results for: {query}"


@activity.defn
async def analyze_activity(data: str) -> str:
    """Simulates an analysis activity."""
    with tracer.start_as_current_span("analyze_work") as span:
        span.set_attribute("data_length", len(data))
        await asyncio.sleep(0.01)
        return f"Analysis of: {data[:50]}..."


@activity.defn
async def write_report_activity(analysis: str) -> str:
    """Simulates writing a report."""
    with tracer.start_as_current_span("write_report_work") as span:
        span.set_attribute("input_length", len(analysis))
        await asyncio.sleep(0.01)
        return f"Report based on: {analysis[:30]}..."


@workflow.defn
class WorkflowWithSpans:
    """A workflow that creates a span and calls multiple activities."""

    @workflow.run
    async def run(self, query: str) -> str:
        # Use workflow_span for replay-safe span creation
        with workflow_span("research_workflow", query=query):
            # Step 1: Research
            research = await workflow.execute_activity(
                research_activity,
                query,
                start_to_close_timeout=timedelta(seconds=30),
            )

            # Step 2: Analyze
            analysis = await workflow.execute_activity(
                analyze_activity,
                research,
                start_to_close_timeout=timedelta(seconds=30),
            )

            # Step 3: Write report
            report = await workflow.execute_activity(
                write_report_activity,
                analysis,
                start_to_close_timeout=timedelta(seconds=30),
            )

        return report


@pytest.mark.asyncio
async def test_workflow_span_no_duplication_during_replay(tracing: InMemorySpanExporter):
    """Test that workflow_span prevents duplicate spans during replay.

    With max_cached_workflows=0:
    - Each activity completion triggers a new workflow task
    - Each workflow task replays from the beginning
    - workflow_span should prevent duplicate workflow spans
    - Activity spans should appear exactly once (no replay)
    """
    async with await WorkflowEnvironment.start_local() as env:
        interceptor = TracingInterceptor(create_spans=False)
        client_config = env.client.config()
        client_config["interceptors"] = [interceptor]
        client = Client(**client_config)

        task_queue = f"test-e2e-replay-{uuid.uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[WorkflowWithSpans],
            activities=[research_activity, analyze_activity, write_report_activity],
            interceptors=[interceptor],
            workflow_runner=UnsandboxedWorkflowRunner(),
            max_cached_workflows=0,  # Force replay on every task
        ):
            # Create a client-side root span to establish trace context
            with tracer.start_as_current_span("client_request") as root:
                root.set_attribute("query", "Test query")
                result = await client.execute_workflow(
                    WorkflowWithSpans.run,
                    "Test query",
                    id=f"wf-e2e-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    assert "Report based on" in result

    await asyncio.sleep(0.3)
    spans = tracing.get_finished_spans()

    # Count spans by name
    span_counts: dict[str, int] = {}
    for s in spans:
        span_counts[s.name] = span_counts.get(s.name, 0) + 1

    # Verify workflow span appears exactly once (replay-safe)
    assert span_counts.get("research_workflow", 0) == 1, (
        f"Expected 1 research_workflow span, got {span_counts.get('research_workflow', 0)}. "
        f"workflow_span may not be working correctly."
    )

    # Verify each activity span appears exactly once
    assert span_counts.get("research_work", 0) == 1, "research_work should appear once"
    assert span_counts.get("analyze_work", 0) == 1, "analyze_work should appear once"
    assert span_counts.get("write_report_work", 0) == 1, "write_report_work should appear once"

    # Verify no unexpected duplication
    duplicated = {name: count for name, count in span_counts.items() if count > 1}
    assert not duplicated, f"Unexpected span duplication: {duplicated}"

    # Verify all spans share the same trace ID
    trace_ids = set(s.context.trace_id for s in spans)
    assert len(trace_ids) == 1, f"Expected 1 trace_id, got {len(trace_ids)}"
