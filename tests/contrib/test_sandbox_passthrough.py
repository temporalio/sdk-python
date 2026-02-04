"""Tests for OpenTelemetry sandbox passthrough behavior.

These tests verify that OTEL context propagates correctly when opentelemetry
is configured as a passthrough module in the sandbox.
"""

import asyncio
import concurrent.futures
import multiprocessing
import threading
import uuid
from datetime import timedelta
from multiprocessing import Queue
from typing import Any

import pytest

import opentelemetry.trace

from temporalio import activity, workflow
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


# Global barrier for concurrent workflow synchronization
_concurrent_barrier: threading.Barrier | None = None


@activity.defn
def barrier_sync_activity(workflow_index: int) -> None:
    """Activity that waits at barrier to ensure concurrent execution.

    All concurrent workflows will block here until all have reached this point,
    guaranteeing true concurrent execution for the test.
    """
    assert _concurrent_barrier is not None, "Barrier must be set before test"
    _concurrent_barrier.wait()


@workflow.defn
class ConcurrentTraceContextWorkflow:
    """Workflow that captures trace context before and after a barrier activity."""

    @workflow.run
    async def run(self, workflow_index: int) -> dict[str, Any]:
        # Capture trace context BEFORE activity (set by interceptor at workflow start)
        span_before = opentelemetry.trace.get_current_span()
        ctx_before = span_before.get_span_context()
        trace_id_before = ctx_before.trace_id if ctx_before else 0

        # Call activity - all workflows block at barrier until all arrive
        await workflow.execute_activity(
            barrier_sync_activity,
            workflow_index,
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Capture trace context AFTER activity (should still be the same)
        span_after = opentelemetry.trace.get_current_span()
        ctx_after = span_after.get_span_context()
        trace_id_after = ctx_after.trace_id if ctx_after else 0

        return {
            "workflow_index": workflow_index,
            "trace_id_before": trace_id_before,
            "trace_id_after": trace_id_after,
            "is_valid": span_before is not opentelemetry.trace.INVALID_SPAN,
        }


@pytest.mark.asyncio
async def test_concurrent_workflows_isolated_trace_context(
    tracer_provider_and_exporter,
):
    """Test that concurrent workflows each see their own trace context.

    This test verifies that when multiple workflows run concurrently with
    opentelemetry passthrough enabled, each workflow sees only its own trace
    context and not another workflow's. Uses a threading.Barrier to ensure
    all workflows are truly executing concurrently (not timing-based).
    """
    global _concurrent_barrier
    num_workflows = 5
    _concurrent_barrier = threading.Barrier(num_workflows)

    provider, exporter = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    interceptor = TracingInterceptor()

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"test-queue-{uuid.uuid4()}"

        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            interceptors=[interceptor],
        )

        # Use ThreadPoolExecutor for sync activity
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workflows
        ) as executor:
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[ConcurrentTraceContextWorkflow],
                activities=[barrier_sync_activity],
                activity_executor=executor,
                interceptors=[interceptor],
                workflow_runner=SandboxedWorkflowRunner(
                    restrictions=SandboxRestrictions.default.with_passthrough_modules(
                        "opentelemetry"
                    )
                ),
            ):

                async def run_workflow_with_trace(index: int) -> tuple[int, dict, int]:
                    """Start a workflow under its own trace context."""
                    with tracer.start_as_current_span(f"trace_{index}") as span:
                        expected_trace_id = span.get_span_context().trace_id
                        result = await client.execute_workflow(
                            ConcurrentTraceContextWorkflow.run,
                            index,
                            id=f"concurrent-test-{index}-{uuid.uuid4()}",
                            task_queue=task_queue,
                        )
                        return (index, result, expected_trace_id)

                # Launch all workflows concurrently
                results = await asyncio.gather(
                    *[run_workflow_with_trace(i) for i in range(num_workflows)]
                )

            # Verify each workflow saw its own trace_id (not another workflow's)
            for index, result, expected_trace_id in results:
                assert result["is_valid"], f"Workflow {index} should see valid span"
                assert result["trace_id_before"] == expected_trace_id, (
                    f"Workflow {index} saw wrong trace_id before activity: "
                    f"expected {expected_trace_id}, got {result['trace_id_before']}"
                )
                assert result["trace_id_after"] == expected_trace_id, (
                    f"Workflow {index} saw wrong trace_id after activity: "
                    f"expected {expected_trace_id}, got {result['trace_id_after']}"
                )

            # Verify all trace_ids were different (each workflow had unique trace)
            trace_ids = [r[2] for r in results]
            assert len(set(trace_ids)) == num_workflows, (
                f"All {num_workflows} workflows should have unique traces, "
                f"but got {len(set(trace_ids))} unique trace_ids"
            )


# =============================================================================
# Cross-Process Trace Continuity Test
# =============================================================================


@activity.defn
async def record_trace_with_child_span(label: str) -> dict[str, Any]:
    """Activity that creates a child span and records parent span info.

    This activity captures the current span context (propagated via headers)
    and creates a child span to verify the parent span_id is correct.
    """
    from opentelemetry.sdk.trace import ReadableSpan

    tracer = opentelemetry.trace.get_tracer(__name__)

    # Get the current span context (propagated via Temporal headers)
    current_span = opentelemetry.trace.get_current_span()
    current_ctx = current_span.get_span_context()

    # Create a child span to verify parent relationship
    with tracer.start_as_current_span(f"child_{label}") as child:
        child_ctx = child.get_span_context()
        # Get the parent span context from the child
        # ReadableSpan (from SDK) has parent attribute, NonRecordingSpan doesn't
        if isinstance(child, ReadableSpan):
            parent_ctx = child.parent
        else:
            # NonRecordingSpan - fall back to current span context
            parent_ctx = current_ctx

    return {
        "label": label,
        "trace_id": current_ctx.trace_id if current_ctx else 0,
        "current_span_id": current_ctx.span_id if current_ctx else 0,
        "child_span_id": child_ctx.span_id if child_ctx else 0,
        "child_parent_span_id": parent_ctx.span_id if parent_ctx else 0,
        "is_valid": current_span is not opentelemetry.trace.INVALID_SPAN,
    }


@workflow.defn
class CrossProcessTraceWorkflow:
    """Workflow that records trace info in two parts, separated by a signal."""

    def __init__(self) -> None:
        self._continue = False

    @workflow.signal
    def continue_workflow(self) -> None:
        """Signal to continue to part 2."""
        self._continue = True

    @workflow.run
    async def run(self) -> dict[str, Any]:
        # Part 1: Record trace on first worker
        trace_part1 = await workflow.execute_activity(
            record_trace_with_child_span,
            "part1",
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Wait for signal (worker swap happens here)
        await workflow.wait_condition(lambda: self._continue)

        # Part 2: Record trace on second worker (different process)
        trace_part2 = await workflow.execute_activity(
            record_trace_with_child_span,
            "part2",
            start_to_close_timeout=timedelta(seconds=30),
        )

        return {"part1": trace_part1, "part2": trace_part2}


def _run_worker_in_subprocess(
    target_host: str,
    namespace: str,
    task_queue: str,
    ready_queue: Queue,
    stop_queue: Queue,
) -> None:
    """Entry point for worker subprocess.

    Runs in a completely separate process with fresh Python interpreter.
    """
    asyncio.run(
        _run_worker_async(target_host, namespace, task_queue, ready_queue, stop_queue)
    )


async def _run_worker_async(
    target_host: str,
    namespace: str,
    task_queue: str,
    ready_queue: Queue,
    stop_queue: Queue,
) -> None:
    """Async worker runner for subprocess."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Set up TracerProvider in subprocess so we get real spans
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    # Fresh interceptor in this process - no shared state with parent
    # Use create_spans=False to just propagate context (like OtelTracingPlugin does)
    interceptor = TracingInterceptor(create_spans=False)

    client = await Client.connect(
        target_host,
        namespace=namespace,
        interceptors=[interceptor],
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[CrossProcessTraceWorkflow],
        activities=[record_trace_with_child_span],
        interceptors=[interceptor],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "opentelemetry"
            )
        ),
    ):
        # Signal that worker is ready
        ready_queue.put("ready")

        # Run until stop signal
        while stop_queue.empty():
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_trace_continuity_across_worker_processes(
    tracer_provider_and_exporter,
):
    """Test trace context survives workflow moving between separate processes.

    This test verifies that trace context is properly serialized into Temporal
    workflow headers and correctly deserialized when a completely different
    worker process picks up the workflow. This is critical because:
    - Workflows can be replayed on different workers
    - Workers can be on different machines
    - In-process ContextVars cannot be relied upon

    The test:
    1. Starts workflow from main process with trace_id X
    2. Worker 1 (subprocess) handles part 1, records trace info
    3. Worker 1 terminates
    4. Worker 2 (new subprocess) starts, handles part 2
    5. Verifies both parts saw the same trace_id AND parent span_id
    """
    provider, exporter = tracer_provider_and_exporter
    tracer = opentelemetry.trace.get_tracer(__name__)

    # Use spawn context for true process isolation (not fork)
    mp_context = multiprocessing.get_context("spawn")

    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"cross-process-{uuid.uuid4()}"
        target_host = env.client.service_client.config.target_host
        namespace = env.client.namespace

        # Create queues for process coordination
        ready_queue1: Queue = mp_context.Queue()
        stop_queue1: Queue = mp_context.Queue()
        ready_queue2: Queue = mp_context.Queue()
        stop_queue2: Queue = mp_context.Queue()

        # Start Worker 1 in subprocess
        worker1_process = mp_context.Process(
            target=_run_worker_in_subprocess,
            args=(target_host, namespace, task_queue, ready_queue1, stop_queue1),
        )
        worker1_process.start()

        try:
            # Wait for worker 1 to be ready
            ready_queue1.get(timeout=30)

            # Create client with tracing interceptor
            # Use create_spans=False (like OtelTracingPlugin) - just propagate context
            interceptor = TracingInterceptor(create_spans=False)
            client = await Client.connect(
                target_host,
                namespace=namespace,
                interceptors=[interceptor],
            )

            # Start workflow under a trace context
            with tracer.start_as_current_span("root_trace") as root_span:
                root_ctx = root_span.get_span_context()
                expected_trace_id = root_ctx.trace_id
                expected_span_id = root_ctx.span_id

                handle = await client.start_workflow(
                    CrossProcessTraceWorkflow.run,
                    id=f"cross-process-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

            # Wait for part 1 to complete (workflow now waiting for signal)
            # Poll workflow state or just wait a bit
            await asyncio.sleep(2)

            # Stop Worker 1
            stop_queue1.put("stop")
            worker1_process.join(timeout=10)

            # Start Worker 2 in NEW subprocess (completely fresh process)
            worker2_process = mp_context.Process(
                target=_run_worker_in_subprocess,
                args=(target_host, namespace, task_queue, ready_queue2, stop_queue2),
            )
            worker2_process.start()

            # Wait for worker 2 to be ready
            ready_queue2.get(timeout=30)

            # Signal workflow to continue
            await handle.signal(CrossProcessTraceWorkflow.continue_workflow)

            # Get result
            result = await handle.result()

            # Stop Worker 2
            stop_queue2.put("stop")
            worker2_process.join(timeout=10)

        finally:
            # Cleanup: ensure processes are terminated
            if worker1_process.is_alive():
                worker1_process.terminate()
                worker1_process.join(timeout=5)
            if "worker2_process" in locals() and worker2_process.is_alive():
                worker2_process.terminate()
                worker2_process.join(timeout=5)

        # Verify trace continuity
        part1 = result["part1"]
        part2 = result["part2"]

        # Both parts should see valid spans
        assert part1["is_valid"], "Part 1 should see valid span"
        assert part2["is_valid"], "Part 2 should see valid span"

        # Both parts should see the same trace_id
        assert part1["trace_id"] == expected_trace_id, (
            f"Part 1 trace_id mismatch: expected {expected_trace_id}, "
            f"got {part1['trace_id']}"
        )
        assert part2["trace_id"] == expected_trace_id, (
            f"Part 2 trace_id mismatch: expected {expected_trace_id}, "
            f"got {part2['trace_id']}"
        )

        # Both parts should see the same current span_id (the workflow span)
        assert part1["current_span_id"] == part2["current_span_id"], (
            f"Current span_id should be same across processes: "
            f"part1={part1['current_span_id']}, part2={part2['current_span_id']}"
        )

        # Child spans created in both workers should have the same parent
        assert part1["child_parent_span_id"] == part2["child_parent_span_id"], (
            f"Child spans should have same parent span_id: "
            f"part1={part1['child_parent_span_id']}, "
            f"part2={part2['child_parent_span_id']}"
        )

        # The child's parent should be the current span
        assert part1["child_parent_span_id"] == part1["current_span_id"], (
            f"Child's parent should be current span: "
            f"child_parent={part1['child_parent_span_id']}, "
            f"current={part1['current_span_id']}"
        )
