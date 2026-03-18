"""Integration tests for LangSmith plugin with real Temporal worker."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import nexusrpc.handler
import pytest
from langsmith import traceable, tracing_context

from temporalio import activity, common, nexus, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.langsmith import LangSmithPlugin
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.conftest import InMemoryRunCollector, dump_runs
from tests.helpers import new_worker
from tests.helpers.nexus import make_nexus_endpoint_name

# ---------------------------------------------------------------------------
# Shared @traceable functions and activities
# ---------------------------------------------------------------------------


@traceable(name="inner_llm_call")
async def _inner_llm_call(prompt: str) -> str:
    """Simulates an LLM call decorated with @traceable."""
    return f"response to: {prompt}"


@traceable(name="outer_chain")
async def _outer_chain(prompt: str) -> str:
    """A @traceable that calls another @traceable."""
    return await _inner_llm_call(prompt)


@activity.defn
async def traceable_activity() -> str:
    """Activity that calls a @traceable function."""
    result = await _inner_llm_call("hello")
    return result


@activity.defn
async def nested_traceable_activity() -> str:
    """Activity with two levels of @traceable nesting."""
    result = await _outer_chain("hello")
    return result


# ---------------------------------------------------------------------------
# Shared workflows
# ---------------------------------------------------------------------------


@workflow.defn
class TraceableActivityWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class SimpleNexusWorkflow:
    @workflow.run
    async def run(self, _input: str) -> str:
        return await workflow.execute_activity(
            traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


@nexusrpc.handler.service_handler
class NexusService:
    @nexus.workflow_run_operation
    async def run_operation(
        self, ctx: nexus.WorkflowRunOperationContext, input: str
    ) -> nexus.WorkflowHandle[str]:
        return await ctx.start_workflow(
            SimpleNexusWorkflow.run,
            input,
            id=f"nexus-wf-{ctx.request_id}",
        )


# ---------------------------------------------------------------------------
# Simple/basic workflows and activities
# ---------------------------------------------------------------------------


@activity.defn
async def simple_activity() -> str:
    return "activity-done"


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        result = await workflow.execute_activity(
            simple_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        return result


# ---------------------------------------------------------------------------
# Signal/query/update workflows
# ---------------------------------------------------------------------------


@workflow.defn
class ComprehensiveWorkflow:
    def __init__(self) -> None:
        self._signal_received = False
        self._complete = False

    @workflow.run
    async def run(self) -> str:
        # 1. Regular activity
        await workflow.execute_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        # 2. Local activity
        await workflow.execute_local_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        # 3. Child workflow
        await workflow.execute_child_workflow(
            TraceableActivityWorkflow.run,
            id=f"child-{workflow.info().workflow_id}",
        )
        # 4. Nexus operation
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=NexusService,
        )
        nexus_handle = await nexus_client.start_operation(
            operation=NexusService.run_operation,
            input="test-input",
        )
        await nexus_handle
        # 5. Wait for signal
        await workflow.wait_condition(lambda: self._signal_received)
        # 5. Wait for update to complete
        await workflow.wait_condition(lambda: self._complete)
        return "comprehensive-done"

    @workflow.signal
    def my_signal(self, _value: str) -> None:
        self._signal_received = True

    @workflow.query
    def my_query(self) -> bool:
        return self._signal_received

    @workflow.update
    def my_update(self, value: str) -> str:
        self._complete = True
        return f"updated-{value}"

    @my_update.validator
    def validate_my_update(self, value: str) -> None:
        if not value:
            raise ValueError("empty")


# ---------------------------------------------------------------------------
# Error workflows and activities
# ---------------------------------------------------------------------------


@activity.defn
async def failing_activity() -> str:
    raise ApplicationError("activity-failed", non_retryable=True)


@activity.defn
async def benign_failing_activity() -> str:
    from temporalio.exceptions import ApplicationErrorCategory

    raise ApplicationError(
        "benign-fail",
        non_retryable=True,
        category=ApplicationErrorCategory.BENIGN,
    )


@workflow.defn
class FailingWorkflow:
    @workflow.run
    async def run(self) -> str:
        raise ApplicationError("workflow-failed", non_retryable=True)


@workflow.defn
class ActivityFailureWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            failing_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=common.RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class BenignErrorWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            benign_failing_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=common.RetryPolicy(maximum_attempts=1),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plugin_and_collector(
    **kwargs: Any,
) -> tuple[LangSmithPlugin, InMemoryRunCollector, MagicMock]:
    """Create a LangSmithPlugin wired to an InMemoryRunCollector via mock client."""
    collector = InMemoryRunCollector()
    mock_ls_client = MagicMock()
    mock_ls_client.create_run.side_effect = collector.record_create
    mock_ls_client.update_run.side_effect = collector.record_update
    mock_ls_client.session = MagicMock()
    mock_ls_client.tracing_queue = MagicMock()
    plugin = LangSmithPlugin(client=mock_ls_client, **kwargs)
    return plugin, collector, mock_ls_client


def _make_client_and_collector(
    client: Client, **kwargs: Any
) -> tuple[Client, InMemoryRunCollector, MagicMock]:
    """Create a Temporal Client with LangSmith plugin and an InMemoryRunCollector."""
    plugin, collector, mock_ls_client = _make_plugin_and_collector(**kwargs)
    config = client.config()
    config["plugins"] = [plugin]
    return Client(**config), collector, mock_ls_client


# ---------------------------------------------------------------------------
# TestBasicTracing
# ---------------------------------------------------------------------------


class TestBasicTracing:
    async def test_workflow_activity_trace_hierarchy(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """StartWorkflow → RunWorkflow → StartActivity → RunActivity hierarchy."""
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            SimpleWorkflow,
            activities=[simple_activity],
        ) as worker:
            result = await temporal_client.start_workflow(
                SimpleWorkflow.run,
                id=f"basic-trace-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert await result.result() == "activity-done"

        hierarchy = dump_runs(collector)
        expected = [
            "StartWorkflow:SimpleWorkflow",
            "  RunWorkflow:SimpleWorkflow",
            "    StartActivity:simple_activity",
            "      RunActivity:simple_activity",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"

        # Verify run_type: RunActivity is "tool", others are "chain"
        for run in collector.runs:
            if run.name == "RunActivity:simple_activity":
                assert (
                    run.run_type == "tool"
                ), f"Expected RunActivity run_type='tool', got '{run.run_type}'"
            else:
                assert (
                    run.run_type == "chain"
                ), f"Expected {run.name} run_type='chain', got '{run.run_type}'"

        # Verify successful runs have outputs == {"status": "ok"}
        for run in collector.runs:
            assert run.outputs == {
                "status": "ok"
            }, f"Expected {run.name} outputs={{'status': 'ok'}}, got {run.outputs}"


# ---------------------------------------------------------------------------
# TestReplay
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_no_duplicate_traces_on_replay(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """With max_cached_workflows=0 (forcing replay), no duplicate runs appear."""
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            TraceableActivityWorkflow,
            activities=[traceable_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                TraceableActivityWorkflow.run,
                id=f"replay-test-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle.result()

        # Workflow→activity→@traceable flow should produce exactly these runs
        # with no duplicates from replay:
        hierarchy = dump_runs(collector)
        expected = [
            "StartWorkflow:TraceableActivityWorkflow",
            "  RunWorkflow:TraceableActivityWorkflow",
            "    StartActivity:traceable_activity",
            "      RunActivity:traceable_activity",
            "        inner_llm_call",
        ]
        assert hierarchy == expected, (
            f"Hierarchy mismatch (possible replay duplicates).\n"
            f"Expected:\n{expected}\nActual:\n{hierarchy}"
        )


# ---------------------------------------------------------------------------
# TestErrorTracing
# ---------------------------------------------------------------------------


class TestErrorTracing:
    async def test_activity_failure_marked(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """A failing activity run is marked with an error."""
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            ActivityFailureWorkflow,
            activities=[failing_activity],
            workflow_failure_exception_types=[ApplicationError],
        ) as worker:
            handle = await temporal_client.start_workflow(
                ActivityFailureWorkflow.run,
                id=f"act-fail-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()

        hierarchy = dump_runs(collector)
        expected = [
            "StartWorkflow:ActivityFailureWorkflow",
            "  RunWorkflow:ActivityFailureWorkflow",
            "    StartActivity:failing_activity",
            "      RunActivity:failing_activity",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"
        # Verify the RunActivity run has an error
        activity_runs = [
            r for r in collector.runs if r.name == "RunActivity:failing_activity"
        ]
        assert len(activity_runs) == 1
        assert activity_runs[0].error == "ApplicationError: activity-failed"

    async def test_workflow_failure_marked(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """A failing workflow run is marked with an error."""
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            FailingWorkflow,
            workflow_failure_exception_types=[ApplicationError],
        ) as worker:
            handle = await temporal_client.start_workflow(
                FailingWorkflow.run,
                id=f"wf-fail-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()

        hierarchy = dump_runs(collector)
        expected = [
            "StartWorkflow:FailingWorkflow",
            "  RunWorkflow:FailingWorkflow",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"
        # Verify the RunWorkflow run has an error
        wf_runs = [r for r in collector.runs if r.name == "RunWorkflow:FailingWorkflow"]
        assert len(wf_runs) == 1
        assert wf_runs[0].error == "ApplicationError: workflow-failed"

    async def test_benign_error_not_marked(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """A benign ApplicationError does NOT mark the run as errored."""
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            BenignErrorWorkflow,
            activities=[benign_failing_activity],
            workflow_failure_exception_types=[ApplicationError],
        ) as worker:
            handle = await temporal_client.start_workflow(
                BenignErrorWorkflow.run,
                id=f"benign-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()

        hierarchy = dump_runs(collector)
        expected = [
            "StartWorkflow:BenignErrorWorkflow",
            "  RunWorkflow:BenignErrorWorkflow",
            "    StartActivity:benign_failing_activity",
            "      RunActivity:benign_failing_activity",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"
        # The RunActivity run for benign error should NOT have error set
        activity_runs = [
            r for r in collector.runs if r.name == "RunActivity:benign_failing_activity"
        ]
        assert len(activity_runs) == 1
        assert activity_runs[0].error is None


# ---------------------------------------------------------------------------
# TestComprehensiveTracing
# ---------------------------------------------------------------------------


class TestComprehensiveTracing:
    async def test_comprehensive_with_temporal_runs(
        self, client: Client, env: WorkflowEnvironment
    ) -> None:
        """Full workflow exercising activity, local activity, child workflow,
        signal, query, and update — all nested under an ambient @traceable.
        """
        temporal_client, collector, mock_ls_client = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        @traceable(name="user_pipeline")
        async def user_pipeline() -> str:
            async with new_worker(
                temporal_client,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                SimpleNexusWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
            ) as worker:
                await env.create_nexus_endpoint(
                    make_nexus_endpoint_name(worker.task_queue),
                    worker.task_queue,
                )
                handle = await temporal_client.start_workflow(
                    ComprehensiveWorkflow.run,
                    id=f"comprehensive-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                # Query
                await handle.query(ComprehensiveWorkflow.my_query)
                # Signal
                await handle.signal(ComprehensiveWorkflow.my_signal, "hello")
                # Update (completes the workflow)
                await handle.execute_update(ComprehensiveWorkflow.my_update, "finish")
                return await handle.result()

        with tracing_context(client=mock_ls_client, enabled=True):
            result = await user_pipeline()

        assert result == "comprehensive-done"

        hierarchy = dump_runs(collector)
        expected = [
            "user_pipeline",
            "  StartWorkflow:ComprehensiveWorkflow",
            "    RunWorkflow:ComprehensiveWorkflow",
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          outer_chain",
            "            inner_llm_call",
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          outer_chain",
            "            inner_llm_call",
            "      StartChildWorkflow:TraceableActivityWorkflow",
            "        RunWorkflow:TraceableActivityWorkflow",
            "          StartActivity:traceable_activity",
            "            RunActivity:traceable_activity",
            "              inner_llm_call",
            "      StartNexusOperation:NexusService/run_operation",
            "        RunStartNexusOperationHandler:NexusService/run_operation",
            "          StartWorkflow:SimpleNexusWorkflow",
            "            RunWorkflow:SimpleNexusWorkflow",
            "              StartActivity:traceable_activity",
            "                RunActivity:traceable_activity",
            "                  inner_llm_call",
            "  QueryWorkflow:my_query",
            "    HandleQuery:my_query",
            "  SignalWorkflow:my_signal",
            "    HandleSignal:my_signal",
            "  StartWorkflowUpdate:my_update",
            "    ValidateUpdate:my_update",
            "    HandleUpdate:my_update",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"

    async def test_comprehensive_without_temporal_runs(
        self, client: Client, env: WorkflowEnvironment
    ) -> None:
        """With add_temporal_runs=False, only @traceable runs appear,
        all nested under the ambient user_pipeline.
        """
        temporal_client, collector, mock_ls_client = _make_client_and_collector(
            client, add_temporal_runs=False
        )

        @traceable(name="user_pipeline")
        async def user_pipeline() -> str:
            async with new_worker(
                temporal_client,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                SimpleNexusWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
            ) as worker:
                await env.create_nexus_endpoint(
                    make_nexus_endpoint_name(worker.task_queue),
                    worker.task_queue,
                )
                handle = await temporal_client.start_workflow(
                    ComprehensiveWorkflow.run,
                    id=f"comprehensive-no-runs-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                # Query
                await handle.query(ComprehensiveWorkflow.my_query)
                # Signal
                await handle.signal(ComprehensiveWorkflow.my_signal, "hello")
                # Update (completes the workflow)
                await handle.execute_update(ComprehensiveWorkflow.my_update, "finish")
                return await handle.result()

        with tracing_context(client=mock_ls_client, enabled=True):
            result = await user_pipeline()

        assert result == "comprehensive-done"

        hierarchy = dump_runs(collector)
        expected = [
            "user_pipeline",
            "  outer_chain",
            "    inner_llm_call",
            "  outer_chain",
            "    inner_llm_call",
            "  inner_llm_call",
            "  inner_llm_call",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"
