import logging
import uuid
from datetime import timedelta
from typing import Any

import nexusrpc
import opentelemetry.trace
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import (
    get_tracer,
)
from opentelemetry.util._once import Once

import temporalio.contrib.opentelemetry.workflow
from temporalio import activity, nexus, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

# Import the dump_spans function from the original opentelemetry test
from tests.contrib.opentelemetry.test_opentelemetry import dump_spans
from tests.helpers import new_worker
from tests.helpers.nexus import make_nexus_endpoint_name

logger = logging.getLogger(__name__)


@pytest.fixture
def reset_otel_tracer_provider():
    """Reset OpenTelemetry tracer provider state to allow multiple test runs."""
    opentelemetry.trace._TRACER_PROVIDER_SET_ONCE = Once()
    opentelemetry.trace._TRACER_PROVIDER = None
    yield
    opentelemetry.trace._TRACER_PROVIDER_SET_ONCE = Once()
    opentelemetry.trace._TRACER_PROVIDER = None


@activity.defn
async def simple_no_context_activity() -> str:
    with get_tracer(__name__).start_as_current_span("Activity"):
        pass
    return "success"


@workflow.defn
class SimpleNexusWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return f"nexus-result-{input}"


@nexusrpc.handler.service_handler
class ComprehensiveNexusService:
    @nexus.workflow_run_operation
    async def test_operation(
        self, ctx: nexus.WorkflowRunOperationContext, input: str
    ) -> nexus.WorkflowHandle[str]:
        return await ctx.start_workflow(
            SimpleNexusWorkflow.run,
            input,
            id=f"nexus-wf-{ctx.request_id}",
        )


@workflow.defn
class BasicTraceWorkflow:
    @workflow.run
    async def run(self):
        tracer = get_tracer(__name__)
        temporalio.contrib.opentelemetry.workflow.completed_span("Completed Span")
        with tracer.start_as_current_span("Hello World"):
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                simple_no_context_activity,
                start_to_close_timeout=timedelta(seconds=10),
            )
            span = tracer.start_span("Not context")
            with tracer.start_as_current_span("Inner"):
                await workflow.execute_activity(
                    simple_no_context_activity,
                    start_to_close_timeout=timedelta(seconds=10),
                )
            span.end()
        return


async def test_otel_tracing_basic(client: Client, reset_otel_tracer_provider: Any):  # type: ignore[reportUnusedParameter]
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    new_config = client.config()
    new_config["plugins"] = [OpenTelemetryPlugin()]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        BasicTraceWorkflow,
        activities=[simple_no_context_activity],
        max_cached_workflows=0,
    ) as worker:
        tracer = get_tracer(__name__)

        with tracer.start_as_current_span("Research workflow"):
            workflow_handle = await new_client.start_workflow(
                BasicTraceWorkflow.run,
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            await workflow_handle.result()

    spans = exporter.get_finished_spans()

    expected_hierarchy = [
        "Research workflow",
        "  Completed Span",
        "  Hello World",
        "    Activity",
        "    Activity",
        "    Inner",
        "      Activity",
        "    Not context",
    ]

    # Verify the span hierarchy matches expectations
    actual_hierarchy = dump_spans(spans, with_attributes=False)
    assert (
        actual_hierarchy == expected_hierarchy
    ), f"Span hierarchy mismatch.\nExpected:\n{expected_hierarchy}\nActual:\n{actual_hierarchy}"


@workflow.defn
class ComprehensiveWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0
        self._update_completed = False
        self._nexus_result: str = ""

    @workflow.run
    async def run(self, actions: list[str]) -> dict[str, str]:
        results = {}
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("MainWorkflow"):
            for action in actions:
                if action == "activity":
                    with tracer.start_as_current_span("ActivitySection"):
                        result = await workflow.execute_activity(
                            simple_no_context_activity,
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        results["activity"] = result

                elif action == "local_activity":
                    with tracer.start_as_current_span("LocalActivitySection"):
                        result = await workflow.execute_local_activity(
                            simple_no_context_activity,
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        results["local_activity"] = result

                elif action == "child_workflow":
                    with tracer.start_as_current_span("ChildWorkflowSection"):
                        child_handle = await workflow.start_child_workflow(
                            BasicTraceWorkflow.run,
                            id=f"child-{workflow.info().workflow_id}",
                        )

                        await child_handle
                        results["child_workflow"] = "completed"

                elif action == "timer":
                    with tracer.start_as_current_span("TimerSection"):
                        await workflow.sleep(0.01)
                        results["timer"] = "completed"

                elif action == "wait_signal":
                    with tracer.start_as_current_span("WaitSignalSection"):
                        await workflow.wait_condition(lambda: self._signal_count > 0)
                        results["wait_signal"] = (
                            f"received_{self._signal_count}_signals"
                        )

                elif action == "wait_update":
                    with tracer.start_as_current_span("WaitUpdateSection"):
                        await workflow.wait_condition(lambda: self._update_completed)
                        results["wait_update"] = "update_received"

                elif action == "nexus":
                    with tracer.start_as_current_span("NexusSection"):
                        nexus_client = workflow.create_nexus_client(
                            endpoint=make_nexus_endpoint_name(
                                workflow.info().task_queue
                            ),
                            service=ComprehensiveNexusService,
                        )
                        nexus_handle = await nexus_client.start_operation(
                            operation=ComprehensiveNexusService.test_operation,
                            input="test-input",
                        )
                        nexus_result = await nexus_handle
                        results["nexus"] = nexus_result

                elif action == "continue_as_new":
                    with tracer.start_as_current_span("ContinueAsNewSection"):
                        if (
                            len(results) > 0
                        ):  # Only continue as new if we've done some work
                            workflow.continue_as_new(
                                []
                            )  # Empty actions to finish quickly
                        results["continue_as_new"] = "prepared"

        return results

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "signal_count": self._signal_count,
            "update_completed": self._update_completed,
        }

    @workflow.signal
    def notify(self, message: str) -> None:  # type: ignore[reportUnusedParameter]
        self._signal_count += 1

    @workflow.update
    def update_status(self, status: str) -> str:
        self._update_completed = True
        return f"updated_to_{status}"

    @update_status.validator
    def validate_update_status(self, status: str) -> None:
        if not status:
            raise ValueError("Status cannot be empty")


async def test_opentelemetry_comprehensive_tracing(
    client: Client,
    env: WorkflowEnvironment,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test OpenTelemetry v2 integration across all workflow operations."""
    if env.supports_time_skipping:
        pytest.skip("Fails on java test server.")

    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    new_config = client.config()
    new_config["plugins"] = [OpenTelemetryPlugin(add_temporal_spans=True)]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        ComprehensiveWorkflow,
        BasicTraceWorkflow,  # For child workflow
        SimpleNexusWorkflow,  # For Nexus operation
        activities=[simple_no_context_activity],
        nexus_service_handlers=[ComprehensiveNexusService()],
        max_cached_workflows=0,
    ) as worker:
        # Create Nexus endpoint for this task queue
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(worker.task_queue), worker.task_queue
        )

        with get_tracer(__name__).start_as_current_span("ComprehensiveTest") as span:
            span.set_attribute("test.type", "comprehensive")

            # Start workflow with various actions
            workflow_handle = await new_client.start_workflow(
                ComprehensiveWorkflow.run,
                [
                    "activity",
                    "local_activity",
                    "child_workflow",
                    "timer",
                    "nexus",
                    "wait_signal",
                    "wait_update",
                ],
                id=f"comprehensive-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )

            logger.info(f"Comprehensive workflow query")

            # Test query
            status = await workflow_handle.query(ComprehensiveWorkflow.get_status)
            assert status["signal_count"] == 0

            logger.info(f"Comprehensive workflow signal")

            # Test signal
            await workflow_handle.signal(ComprehensiveWorkflow.notify, "test-signal-1")
            await workflow_handle.signal(ComprehensiveWorkflow.notify, "test-signal-2")

            logger.info(f"Comprehensive workflow update")

            # Test update
            update_result = await workflow_handle.execute_update(
                ComprehensiveWorkflow.update_status, "active"
            )
            assert update_result == "updated_to_active"

            logger.info(f"Comprehensive workflow get result")

            # Get final result
            result = await workflow_handle.result()

            # Verify results
            expected_keys = {
                "activity",
                "local_activity",
                "child_workflow",
                "timer",
                "nexus",
                "wait_signal",
                "wait_update",
            }
            assert all(key in result for key in expected_keys)
            assert result["activity"] == "success"
            assert result["local_activity"] == "success"
            assert result["child_workflow"] == "completed"
            assert result["timer"] == "completed"
            assert result["nexus"] == "nexus-result-test-input"
            assert result["wait_signal"] == "received_2_signals"
            assert result["wait_update"] == "update_received"

    spans = exporter.get_finished_spans()

    # Note: Even though we call signal twice, dump_spans() deduplicates signal spans
    # as they "can duplicate in rare situations" according to the original test

    expected_hierarchy = [
        "ComprehensiveTest",
        "  StartWorkflow:ComprehensiveWorkflow",
        "    RunWorkflow:ComprehensiveWorkflow",
        "      MainWorkflow",
        "        ActivitySection",
        "          StartActivity:simple_no_context_activity",
        "            RunActivity:simple_no_context_activity",
        "              Activity",
        "        LocalActivitySection",
        "          StartActivity:simple_no_context_activity",
        "            RunActivity:simple_no_context_activity",
        "              Activity",
        "        ChildWorkflowSection",
        "          StartChildWorkflow:BasicTraceWorkflow",
        "            RunWorkflow:BasicTraceWorkflow",
        "              Completed Span",
        "              Hello World",
        "                StartActivity:simple_no_context_activity",
        "                  RunActivity:simple_no_context_activity",
        "                    Activity",
        "                StartActivity:simple_no_context_activity",
        "                  RunActivity:simple_no_context_activity",
        "                    Activity",
        "                Inner",
        "                  StartActivity:simple_no_context_activity",
        "                    RunActivity:simple_no_context_activity",
        "                      Activity",
        "                Not context",
        "        TimerSection",
        "        NexusSection",
        "          StartNexusOperation:ComprehensiveNexusService/test_operation",
        "            RunStartNexusOperationHandler:ComprehensiveNexusService/test_operation",
        "              StartWorkflow:SimpleNexusWorkflow",
        "                RunWorkflow:SimpleNexusWorkflow",
        "        WaitSignalSection",
        "        WaitUpdateSection",
        "  QueryWorkflow:get_status",
        "    HandleQuery:get_status",
        "  SignalWorkflow:notify",
        "    HandleSignal:notify",
        "  StartWorkflowUpdate:update_status",
        "    ValidateUpdate:update_status",
        "    HandleUpdate:update_status",
    ]

    # Verify the span hierarchy matches expectations
    actual_hierarchy = dump_spans(spans, with_attributes=False)
    assert (
        actual_hierarchy == expected_hierarchy
    ), f"Span hierarchy mismatch.\nExpected:\n{expected_hierarchy}\nActual:\n{actual_hierarchy}"


async def test_otel_tracing_with_added_spans(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    plugin = OpenTelemetryPlugin(add_temporal_spans=True)
    new_config = client.config()
    new_config["plugins"] = [plugin]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        BasicTraceWorkflow,
        activities=[simple_no_context_activity],
        max_cached_workflows=0,
    ) as worker:
        with get_tracer(__name__).start_as_current_span("Research workflow"):
            workflow_handle = await new_client.start_workflow(
                BasicTraceWorkflow.run,
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            await workflow_handle.result()

    spans = exporter.get_finished_spans()

    expected_hierarchy = [
        "Research workflow",
        "  StartWorkflow:BasicTraceWorkflow",
        "    RunWorkflow:BasicTraceWorkflow",
        "      Completed Span",
        "      Hello World",
        "        StartActivity:simple_no_context_activity",
        "          RunActivity:simple_no_context_activity",
        "            Activity",
        "        StartActivity:simple_no_context_activity",
        "          RunActivity:simple_no_context_activity",
        "            Activity",
        "        Inner",
        "          StartActivity:simple_no_context_activity",
        "            RunActivity:simple_no_context_activity",
        "              Activity",
        "        Not context",
    ]

    # Verify the span hierarchy matches expectations
    actual_hierarchy = dump_spans(spans, with_attributes=False)
    assert (
        actual_hierarchy == expected_hierarchy
    ), f"Span hierarchy mismatch.\nExpected:\n{expected_hierarchy}\nActual:\n{actual_hierarchy}"


task_fail_once_workflow_has_failed = False


@workflow.defn(sandboxed=False)
class FailingTaskWorkflow:
    @workflow.run
    async def run(self):
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("FailingWorkflowSpan"):
            with tracer.start_as_current_span("FailingWorkflow CompletedSpan"):
                pass
            global task_fail_once_workflow_has_failed
            if not task_fail_once_workflow_has_failed:
                task_fail_once_workflow_has_failed = True
                raise RuntimeError("Intentional workflow task failure")
            task_fail_once_workflow_has_failed = False

        return


async def test_otel_tracing_workflow_task_failure(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test OpenTelemetry behavior when a workflow task fails."""
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    new_config = client.config()
    new_config["plugins"] = [OpenTelemetryPlugin(add_temporal_spans=True)]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        FailingTaskWorkflow,
        max_cached_workflows=0,
    ) as worker:
        with get_tracer(__name__).start_as_current_span("FailingWorkflowTest"):
            workflow_handle = await new_client.start_workflow(
                FailingTaskWorkflow.run,
                id=f"failing-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            await workflow_handle.result()

    spans = exporter.get_finished_spans()

    # Verify the span hierarchy includes the failure, but only once
    # Spans which completed during the failed task will duplicate
    expected_hierarchy = [
        "FailingWorkflowTest",
        "  StartWorkflow:FailingTaskWorkflow",
        "    RunWorkflow:FailingTaskWorkflow",
        "      FailingWorkflowSpan",
        "        FailingWorkflow CompletedSpan",
        "        FailingWorkflow CompletedSpan",
    ]

    actual_hierarchy = dump_spans(spans, with_attributes=False)
    assert (
        actual_hierarchy == expected_hierarchy
    ), f"Span hierarchy mismatch.\nExpected:\n{expected_hierarchy}\nActual:\n{actual_hierarchy}"


@workflow.defn
class FailingWorkflow:
    @workflow.run
    async def run(self):
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("FailingWorkflowSpan"):
            raise ApplicationError("Intentional workflow failure", non_retryable=True)


async def test_otel_tracing_workflow_failure(
    client: Client,
    reset_otel_tracer_provider: Any,  # type: ignore[reportUnusedParameter]
):
    """Test OpenTelemetry behavior when a workflow task fails."""
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace.set_tracer_provider(provider)

    new_config = client.config()
    new_config["plugins"] = [OpenTelemetryPlugin(add_temporal_spans=True)]
    new_client = Client(**new_config)

    async with new_worker(
        new_client,
        FailingWorkflow,
        max_cached_workflows=0,
    ) as worker:
        with get_tracer(__name__).start_as_current_span("FailingWorkflowTest"):
            workflow_handle = await new_client.start_workflow(
                FailingWorkflow.run,
                id=f"failing-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            with pytest.raises(WorkflowFailureError):
                await workflow_handle.result()

    spans = exporter.get_finished_spans()

    # Verify the span hierarchy includes the failure when it fails the whole workflow
    expected_hierarchy = [
        "FailingWorkflowTest",
        "  StartWorkflow:FailingWorkflow",
        "    RunWorkflow:FailingWorkflow",
        "      FailingWorkflowSpan",
    ]

    actual_hierarchy = dump_spans(spans, with_attributes=False)
    assert (
        actual_hierarchy == expected_hierarchy
    ), f"Span hierarchy mismatch.\nExpected:\n{expected_hierarchy}\nActual:\n{actual_hierarchy}"
