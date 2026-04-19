"""Integration tests for LangSmith plugin with real Temporal worker."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import nexusrpc.handler
import pytest
from langsmith import traceable, tracing_context

from temporalio import activity, common, nexus, workflow
from temporalio.client import (
    Client,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryFailedError,
)
from temporalio.contrib.langsmith import LangSmithPlugin
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.conftest import (
    InMemoryRunCollector,
    dump_runs,
    dump_traces,
    find_traces,
    make_mock_ls_client,
)
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


@traceable
@activity.defn
async def traceable_activity() -> str:
    """Activity that calls a @traceable function."""
    result = await _inner_llm_call("hello")
    return result


@traceable
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
    async def run(self, _input: str = "") -> str:
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
            TraceableActivityWorkflow.run,
            input,
            id=f"nexus-wf-{ctx.request_id}",
        )


# ---------------------------------------------------------------------------
# Simple/basic workflows and activities
# ---------------------------------------------------------------------------


@traceable
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


@traceable(name="step_with_activity")
async def _step_with_activity() -> str:
    """A @traceable step that wraps an activity call."""
    return await workflow.execute_activity(
        nested_traceable_activity,
        start_to_close_timeout=timedelta(seconds=10),
    )


@traceable(name="step_with_child_workflow")
async def _step_with_child_workflow() -> str:
    """A @traceable step that wraps a child workflow call."""
    return await workflow.execute_child_workflow(
        TraceableActivityWorkflow.run,
        id=f"step-child-{workflow.info().workflow_id}",
    )


@traceable(name="step_with_nexus")
async def _step_with_nexus() -> str:
    """A @traceable step that wraps a nexus operation."""
    nexus_client = workflow.create_nexus_client(
        endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
        service=NexusService,
    )
    nexus_handle = await nexus_client.start_operation(
        operation=NexusService.run_operation,
        input="test-input",
    )
    return await nexus_handle


@workflow.defn
class ComprehensiveWorkflow:
    def __init__(self) -> None:
        self._signal_received = False
        self._waiting_for_signal = False
        self._complete = False

    @workflow.run
    async def run(self) -> str:
        await workflow.execute_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await _step_with_activity()
        await workflow.execute_local_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await _outer_chain("from-workflow")
        await workflow.execute_child_workflow(
            TraceableActivityWorkflow.run,
            id=f"child-{workflow.info().workflow_id}",
        )
        await _step_with_child_workflow()
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=NexusService,
        )
        nexus_handle = await nexus_client.start_operation(
            operation=NexusService.run_operation,
            input="test-input",
        )
        await nexus_handle
        await _step_with_nexus()

        self._waiting_for_signal = True
        await workflow.wait_condition(lambda: self._signal_received)
        await workflow.execute_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.wait_condition(lambda: self._complete)
        return "comprehensive-done"

    @workflow.signal
    def my_signal(self, _value: str) -> None:
        self._signal_received = True

    @workflow.query
    def my_query(self) -> bool:
        return self._signal_received

    @workflow.query
    def is_waiting_for_signal(self) -> bool:
        return self._waiting_for_signal

    @workflow.update
    def my_update(self, value: str) -> str:
        self._complete = True
        return f"updated-{value}"

    @my_update.validator
    def validate_my_update(self, value: str) -> None:
        if not value:
            raise ValueError("empty")

    @workflow.update
    def my_unvalidated_update(self, value: str) -> str:
        return f"unvalidated-{value}"


# ---------------------------------------------------------------------------
# Error workflows and activities
# ---------------------------------------------------------------------------


@traceable
@activity.defn
async def failing_activity() -> str:
    raise ApplicationError("activity-failed", non_retryable=True)


@traceable
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
    mock_ls_client = make_mock_ls_client(collector)
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


def _make_temporal_client(
    client: Client, mock_ls_client: MagicMock, **kwargs: Any
) -> Client:
    """Create a Temporal Client with a fresh LangSmith plugin."""
    plugin = LangSmithPlugin(client=mock_ls_client, **kwargs)
    config = client.config()
    config["plugins"] = [plugin]
    return Client(**config)


@traceable(name="poll_query")
async def _poll_query(
    handle: WorkflowHandle[Any, Any],
    query: Callable[..., Any],
    *,
    expected: Any = True,
) -> bool:
    """Poll a workflow query until it returns the expected value."""
    while True:
        try:
            result = await handle.query(query)
            if result == expected:
                return True
        except (WorkflowQueryFailedError, RPCError):
            pass  # Query not yet available (workflow hasn't started)
        await asyncio.sleep(1)


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
            max_cached_workflows=0,
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
            "RunWorkflow:SimpleWorkflow",
            "  StartActivity:simple_activity",
            "  RunActivity:simple_activity",
            "    simple_activity",
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
            if ":" in run.name:  # Interceptor runs use "Type:Name" format
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
            "RunWorkflow:TraceableActivityWorkflow",
            "  StartActivity:traceable_activity",
            "  RunActivity:traceable_activity",
            "    traceable_activity",
            "      inner_llm_call",
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
            max_cached_workflows=0,
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
            "RunWorkflow:ActivityFailureWorkflow",
            "  StartActivity:failing_activity",
            "  RunActivity:failing_activity",
            "    failing_activity",
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
            max_cached_workflows=0,
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
            "RunWorkflow:FailingWorkflow",
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
            max_cached_workflows=0,
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
            "RunWorkflow:BenignErrorWorkflow",
            "  StartActivity:benign_failing_activity",
            "  RunActivity:benign_failing_activity",
            "    benign_failing_activity",
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
        """Full trace hierarchy with worker restart mid-workflow.

        user_pipeline only wraps start_workflow (completing before the worker
        starts), so poll/signal/query traces are naturally separate root traces.
        """
        if env.supports_time_skipping:
            pytest.skip("Time-skipping server doesn't persist headers.")

        task_queue = f"comprehensive-{uuid.uuid4()}"
        workflow_id = f"comprehensive-{uuid.uuid4()}"
        collector = InMemoryRunCollector()
        mock_ls = make_mock_ls_client(collector)
        temporal_client_1 = _make_temporal_client(
            client, mock_ls, add_temporal_runs=True
        )

        @traceable(name="user_pipeline")
        async def user_pipeline() -> WorkflowHandle[Any, Any]:
            return await temporal_client_1.start_workflow(
                ComprehensiveWorkflow.run,
                id=workflow_id,
                task_queue=task_queue,
            )

        with tracing_context(client=mock_ls, enabled=True):
            # Start workflow — no worker yet, just a server RPC
            handle = await user_pipeline()

            # Phase 1: worker picks up workflow, poll until signal wait
            async with new_worker(
                temporal_client_1,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
                task_queue=task_queue,
                max_cached_workflows=0,
            ) as worker:
                await env.create_nexus_endpoint(
                    make_nexus_endpoint_name(worker.task_queue),
                    worker.task_queue,
                )
                assert await _poll_query(
                    handle,
                    ComprehensiveWorkflow.is_waiting_for_signal,
                    expected=True,
                ), "Workflow never reached signal wait point"
                # Raw-client query (no LangSmith interceptor) — root-level trace
                raw_handle = client.get_workflow_handle(workflow_id)
                await raw_handle.query(ComprehensiveWorkflow.is_waiting_for_signal)

            # Phase 2: fresh worker, signal to resume, complete
            temporal_client_2 = _make_temporal_client(
                client, mock_ls, add_temporal_runs=True
            )
            async with new_worker(
                temporal_client_2,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
                task_queue=task_queue,
                max_cached_workflows=0,
            ):
                handle_2 = temporal_client_2.get_workflow_handle(workflow_id)
                await handle_2.query(ComprehensiveWorkflow.my_query)
                await handle_2.signal(ComprehensiveWorkflow.my_signal, "hello")
                await handle_2.execute_update(
                    ComprehensiveWorkflow.my_unvalidated_update, "test"
                )
                await handle_2.execute_update(ComprehensiveWorkflow.my_update, "finish")
                result = await handle_2.result()

        assert result == "comprehensive-done"

        traces = dump_traces(collector)

        # user_pipeline trace: StartWorkflow + full workflow execution tree
        workflow_traces = find_traces(traces, "user_pipeline")
        assert len(workflow_traces) == 1
        assert workflow_traces[0] == [
            "user_pipeline",
            "  StartWorkflow:ComprehensiveWorkflow",
            "  RunWorkflow:ComprehensiveWorkflow",
            "    StartActivity:nested_traceable_activity",
            "    RunActivity:nested_traceable_activity",
            "      nested_traceable_activity",
            "        outer_chain",
            "          inner_llm_call",
            # step-wrapped activity
            "    step_with_activity",
            "      StartActivity:nested_traceable_activity",
            "      RunActivity:nested_traceable_activity",
            "        nested_traceable_activity",
            "          outer_chain",
            "            inner_llm_call",
            "    StartActivity:nested_traceable_activity",
            "    RunActivity:nested_traceable_activity",
            "      nested_traceable_activity",
            "        outer_chain",
            "          inner_llm_call",
            "    outer_chain",
            "      inner_llm_call",
            "    StartChildWorkflow:TraceableActivityWorkflow",
            "    RunWorkflow:TraceableActivityWorkflow",
            "      StartActivity:traceable_activity",
            "      RunActivity:traceable_activity",
            "        traceable_activity",
            "          inner_llm_call",
            # step-wrapped child workflow
            "    step_with_child_workflow",
            "      StartChildWorkflow:TraceableActivityWorkflow",
            "      RunWorkflow:TraceableActivityWorkflow",
            "        StartActivity:traceable_activity",
            "        RunActivity:traceable_activity",
            "          traceable_activity",
            "            inner_llm_call",
            "    StartNexusOperation:NexusService/run_operation",
            "    RunStartNexusOperationHandler:NexusService/run_operation",
            "      StartWorkflow:TraceableActivityWorkflow",
            "      RunWorkflow:TraceableActivityWorkflow",
            "        StartActivity:traceable_activity",
            "        RunActivity:traceable_activity",
            "          traceable_activity",
            "            inner_llm_call",
            # step-wrapped nexus operation
            "    step_with_nexus",
            "      StartNexusOperation:NexusService/run_operation",
            "      RunStartNexusOperationHandler:NexusService/run_operation",
            "        StartWorkflow:TraceableActivityWorkflow",
            "        RunWorkflow:TraceableActivityWorkflow",
            "          StartActivity:traceable_activity",
            "          RunActivity:traceable_activity",
            "            traceable_activity",
            "              inner_llm_call",
            # post-signal
            "    StartActivity:nested_traceable_activity",
            "    RunActivity:nested_traceable_activity",
            "      nested_traceable_activity",
            "        outer_chain",
            "          inner_llm_call",
        ]

        # poll_query trace (separate root, variable number of iterations)
        poll_traces = find_traces(traces, "poll_query")
        assert len(poll_traces) == 1
        poll = poll_traces[0]
        assert poll[0] == "poll_query"
        poll_children = poll[1:]
        for i in range(0, len(poll_children), 2):
            assert poll_children[i] == "  QueryWorkflow:is_waiting_for_signal"
            assert poll_children[i + 1] == "    HandleQuery:is_waiting_for_signal"

        # Raw-client query — no parent context, appears as root
        raw_query_traces = [t for t in traces if t[0].startswith("HandleQuery:")]
        assert len(raw_query_traces) == 1

        # Phase 2: each operation is its own root trace
        query_traces = find_traces(traces, "QueryWorkflow:my_query")
        assert len(query_traces) == 1
        assert query_traces[0] == [
            "QueryWorkflow:my_query",
            "  HandleQuery:my_query",
        ]

        signal_traces = find_traces(traces, "SignalWorkflow:my_signal")
        assert len(signal_traces) == 1
        assert signal_traces[0] == [
            "SignalWorkflow:my_signal",
            "  HandleSignal:my_signal",
        ]

        update_traces = find_traces(traces, "StartWorkflowUpdate:my_update")
        assert len(update_traces) == 1
        assert update_traces[0] == [
            "StartWorkflowUpdate:my_update",
            "  ValidateUpdate:my_update",
            "  HandleUpdate:my_update",
        ]

        # Update without a validator — no ValidateUpdate trace
        unvalidated_traces = find_traces(
            traces, "StartWorkflowUpdate:my_unvalidated_update"
        )
        assert len(unvalidated_traces) == 1
        assert unvalidated_traces[0] == [
            "StartWorkflowUpdate:my_unvalidated_update",
            "  HandleUpdate:my_unvalidated_update",
        ]

    async def test_comprehensive_without_temporal_runs(
        self, client: Client, env: WorkflowEnvironment
    ) -> None:
        """Same workflow with add_temporal_runs=False and worker restart.

        Only @traceable runs appear. Context propagation via headers still works.
        user_pipeline only wraps start_workflow, so poll traces are separate roots.
        """
        if env.supports_time_skipping:
            pytest.skip("Time-skipping server doesn't persist headers.")

        task_queue = f"comprehensive-no-runs-{uuid.uuid4()}"
        workflow_id = f"comprehensive-no-runs-{uuid.uuid4()}"
        collector = InMemoryRunCollector()
        mock_ls = make_mock_ls_client(collector)
        temporal_client_1 = _make_temporal_client(
            client, mock_ls, add_temporal_runs=False
        )

        @traceable(name="user_pipeline")
        async def user_pipeline() -> WorkflowHandle[Any, Any]:
            return await temporal_client_1.start_workflow(
                ComprehensiveWorkflow.run,
                id=workflow_id,
                task_queue=task_queue,
            )

        with tracing_context(client=mock_ls, enabled=True):
            handle = await user_pipeline()

            # Phase 1: worker picks up workflow, poll until signal wait
            async with new_worker(
                temporal_client_1,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
                task_queue=task_queue,
                max_cached_workflows=0,
            ) as worker:
                await env.create_nexus_endpoint(
                    make_nexus_endpoint_name(worker.task_queue),
                    worker.task_queue,
                )
                # Raw-client query — no interceptor, produces nothing
                raw_handle = client.get_workflow_handle(workflow_id)
                await raw_handle.query(ComprehensiveWorkflow.is_waiting_for_signal)
                assert await _poll_query(
                    handle,
                    ComprehensiveWorkflow.is_waiting_for_signal,
                    expected=True,
                ), "Workflow never reached signal wait point"

            # Phase 2: fresh worker, signal to resume, complete
            temporal_client_2 = _make_temporal_client(
                client, mock_ls, add_temporal_runs=False
            )
            async with new_worker(
                temporal_client_2,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
                nexus_service_handlers=[NexusService()],
                task_queue=task_queue,
                max_cached_workflows=0,
            ):
                handle_2 = temporal_client_2.get_workflow_handle(workflow_id)
                await handle_2.signal(ComprehensiveWorkflow.my_signal, "hello")
                await handle_2.execute_update(
                    ComprehensiveWorkflow.my_unvalidated_update, "test"
                )
                await handle_2.execute_update(ComprehensiveWorkflow.my_update, "finish")
                result = await handle_2.result()

        assert result == "comprehensive-done"

        traces = dump_traces(collector)

        # Main workflow trace (only @traceable runs, nested under user_pipeline)
        workflow_traces = find_traces(traces, "user_pipeline")
        assert len(workflow_traces) == 1
        expected_workflow = [
            "user_pipeline",
            "  nested_traceable_activity",
            "    outer_chain",
            "      inner_llm_call",
            "  step_with_activity",
            "    nested_traceable_activity",
            "      outer_chain",
            "        inner_llm_call",
            "  nested_traceable_activity",
            "    outer_chain",
            "      inner_llm_call",
            "  outer_chain",
            "    inner_llm_call",
            "  traceable_activity",
            "    inner_llm_call",
            "  step_with_child_workflow",
            "    traceable_activity",
            "      inner_llm_call",
            "  traceable_activity",
            "    inner_llm_call",
            "  step_with_nexus",
            "    traceable_activity",
            "      inner_llm_call",
            # post-signal
            "  nested_traceable_activity",
            "    outer_chain",
            "      inner_llm_call",
        ]
        assert workflow_traces[0] == expected_workflow, (
            f"Workflow trace mismatch.\n"
            f"Expected:\n{expected_workflow}\nActual:\n{workflow_traces[0]}"
        )

        # Poll query — separate root, just the @traceable wrapper, no Temporal children
        poll_traces = find_traces(traces, "poll_query")
        assert len(poll_traces) == 1
        assert poll_traces[0] == ["poll_query"]


# ---------------------------------------------------------------------------
# TestBackgroundIOIntegration — _RootReplaySafeRunTreeFactory + sync @traceable
# ---------------------------------------------------------------------------


@traceable(name="sync_inner_llm_call")
def _sync_inner_llm_call(prompt: str) -> str:
    """Sync @traceable simulating an LLM call."""
    return f"sync-response to: {prompt}"


@traceable(name="sync_outer_chain")
def _sync_outer_chain(prompt: str) -> str:
    """Sync @traceable that calls another sync @traceable."""
    return _sync_inner_llm_call(prompt)


@traceable(name="async_calls_sync")
async def _async_calls_sync(prompt: str) -> str:
    """Async @traceable that calls a sync @traceable — the interesting mixed case."""
    return _sync_inner_llm_call(prompt)


@workflow.defn
class FactoryTraceableWorkflow:
    """Workflow exercising _RootReplaySafeRunTreeFactory with async, sync, and mixed @traceable.

    Covers three code paths through create_child:
    - async→async nesting
    - sync→sync nesting (sync @traceable entry to factory)
    - async→sync nesting (cross-boundary case)
    """

    @workflow.run
    async def run(self) -> str:
        r1 = await _outer_chain("async")
        r2 = _sync_outer_chain("sync")
        r3 = await _async_calls_sync("mixed")
        # Activity with nested @traceable
        await workflow.execute_activity(
            nested_traceable_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )
        return f"{r1}|{r2}|{r3}"


class TestBackgroundIOIntegration:
    """Integration tests for workflows using add_temporal_runs=False without external context.

    Exercises the _RootReplaySafeRunTreeFactory path with sync, async, and mixed @traceable
    nesting. Verifies root-run creation, correct nesting hierarchy, and replay safety.
    """

    async def test_factory_traceable_no_external_context(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """Exercises _RootReplaySafeRunTreeFactory: add_temporal_runs=False, no external context.

        Uses a workflow with async→async, sync→sync, and async→sync @traceable
        nesting, plus an activity with nested @traceable. Verifies:
        - Each top-level @traceable becomes a root run (factory creates root children)
        - Nested @traceable calls nest correctly under their parent
        - Activity @traceable also produces correct hierarchy
        - No phantom factory run appears in collected runs
        - No duplicate run IDs after replay (max_cached_workflows=0)
        """
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=False
        )

        async with new_worker(
            temporal_client,
            FactoryTraceableWorkflow,
            activities=[nested_traceable_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                FactoryTraceableWorkflow.run,
                id=f"factory-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert (
            result
            == "response to: async|sync-response to: sync|sync-response to: mixed"
        )

        hierarchy = dump_runs(collector)
        expected = [
            "outer_chain",
            "  inner_llm_call",
            "sync_outer_chain",
            "  sync_inner_llm_call",
            "async_calls_sync",
            "  sync_inner_llm_call",
            "nested_traceable_activity",
            "  outer_chain",
            "    inner_llm_call",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"

        # Verify no duplicate run IDs (replay safety with max_cached_workflows=0)
        run_ids = [r.id for r in collector.runs]
        assert len(run_ids) == len(
            set(run_ids)
        ), f"Duplicate run IDs found (replay issue): {run_ids}"

    async def test_factory_passes_project_name_to_children(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """Factory children inherit project_name (session_name) from plugin config."""
        temporal_client, _collector, mock_ls_client = _make_client_and_collector(
            client, add_temporal_runs=False, project_name="my-ls-project"
        )

        async with new_worker(
            temporal_client,
            FactoryTraceableWorkflow,
            activities=[nested_traceable_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                FactoryTraceableWorkflow.run,
                id=f"factory-proj-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await handle.result()

        # Verify create_run calls include session_name from project_name
        for call in mock_ls_client.create_run.call_args_list:
            session = call.kwargs.get("session_name")
            assert session == "my-ls-project", (
                f"Expected session_name='my-ls-project', got {session!r} "
                f"in create_run call: {call.kwargs.get('name')}"
            )

    async def test_mixed_sync_async_traceable_with_temporal_runs(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
    ) -> None:
        """Exercises _ReplaySafeRunTree.create_child with mixed sync/async @traceable.

        With add_temporal_runs=True, the interceptor creates a real
        _ReplaySafeRunTree as parent. This test verifies create_child
        propagation works at every level regardless of sync/async, with
        correct parent-child hierarchy and no duplicate run IDs after replay.
        """
        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            FactoryTraceableWorkflow,
            activities=[nested_traceable_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                FactoryTraceableWorkflow.run,
                id=f"mixed-temporal-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert (
            result
            == "response to: async|sync-response to: sync|sync-response to: mixed"
        )

        hierarchy = dump_runs(collector)
        # With add_temporal_runs=True, Temporal operations get their own runs.
        # @traceable calls nest under the RunWorkflow run.
        expected = [
            "StartWorkflow:FactoryTraceableWorkflow",
            "RunWorkflow:FactoryTraceableWorkflow",
            "  outer_chain",
            "    inner_llm_call",
            "  sync_outer_chain",
            "    sync_inner_llm_call",
            "  async_calls_sync",
            "    sync_inner_llm_call",
            "  StartActivity:nested_traceable_activity",
            "  RunActivity:nested_traceable_activity",
            "    nested_traceable_activity",
            "      outer_chain",
            "        inner_llm_call",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"

        # Verify no duplicate run IDs (replay safety with max_cached_workflows=0)
        run_ids = [r.id for r in collector.runs]
        assert len(run_ids) == len(
            set(run_ids)
        ), f"Duplicate run IDs found (replay issue): {run_ids}"


# --- Nexus service with direct @traceable call in handler ---


@traceable(name="nexus_direct_traceable")
async def _nexus_direct_traceable(input: str) -> str:
    """A @traceable function called directly from a nexus handler."""
    return await _inner_llm_call(input)


@nexusrpc.handler.service_handler
class DirectTraceableNexusService:
    """Nexus service that calls @traceable directly (not via activity)."""

    @nexusrpc.handler.sync_operation
    async def direct_traceable_op(
        self,
        ctx: nexusrpc.handler.StartOperationContext,  # type:ignore[reportUnusedParameter]
        input: str,
    ) -> str:
        return await _nexus_direct_traceable(input)


@workflow.defn
class NexusDirectTraceableWorkflow:
    """Workflow that calls a nexus operation whose handler uses @traceable directly."""

    @workflow.run
    async def run(self) -> str:
        nexus_client = workflow.create_nexus_client(
            endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
            service=DirectTraceableNexusService,
        )
        return await nexus_client.execute_operation(
            operation=DirectTraceableNexusService.direct_traceable_op,
            input="nexus-input",
        )


# ---------------------------------------------------------------------------
# TestNexusInboundTracing
# ---------------------------------------------------------------------------


class TestNexusInboundTracing:
    """Verifies nexus handlers receive tracing_context for @traceable collection."""

    async def test_nexus_direct_traceable_without_temporal_runs(
        self,
        client: Client,
        env: WorkflowEnvironment,
    ) -> None:
        """@traceable in nexus handler works with add_temporal_runs=False.

        The worker must be started OUTSIDE tracing_context so that nexus handler
        tasks inherit a clean contextvars state. Only the client call gets
        tracing_context — the interceptor's tracing_context setup (or lack
        thereof) is the only thing that should provide context to the handler.
        """
        if env.supports_time_skipping:
            pytest.skip("Time-skipping server doesn't persist headers.")

        task_queue = f"nexus-direct-{uuid.uuid4()}"
        collector = InMemoryRunCollector()
        mock_ls = make_mock_ls_client(collector)
        temporal_client = _make_temporal_client(
            client, mock_ls, add_temporal_runs=False
        )

        # Worker starts OUTSIDE tracing_context — nexus handler tasks get clean context
        async with new_worker(
            temporal_client,
            NexusDirectTraceableWorkflow,
            nexus_service_handlers=[DirectTraceableNexusService()],
            task_queue=task_queue,
            max_cached_workflows=0,
        ) as worker:
            await env.create_nexus_endpoint(
                make_nexus_endpoint_name(worker.task_queue),
                worker.task_queue,
            )
            # Only the client call gets tracing context, not the worker
            with tracing_context(client=mock_ls, enabled=True):
                handle = await temporal_client.start_workflow(
                    NexusDirectTraceableWorkflow.run,
                    id=f"nexus-direct-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                result = await handle.result()

        assert result == "response to: nexus-input"

        hierarchy = dump_runs(collector)
        # @traceable runs from inside the nexus handler should be collected
        # via the interceptor's tracing_context setup.
        expected = [
            "nexus_direct_traceable",
            "  inner_llm_call",
        ]
        assert (
            hierarchy == expected
        ), f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"


# ---------------------------------------------------------------------------
# TestBuiltinQueryFiltering
# ---------------------------------------------------------------------------


@workflow.defn
class QueryFilteringWorkflow:
    """Workflow with a user query and a signal to complete."""

    def __init__(self) -> None:
        self._complete = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._complete)
        return "done"

    @workflow.signal
    def complete(self) -> None:
        self._complete = True

    @workflow.query
    def my_query(self) -> str:
        return "query-result"


class TestBuiltinQueryFiltering:
    """Verifies __temporal_ prefixed queries are not traced."""

    async def test_temporal_prefixed_query_not_traced(
        self,
        client: Client,
    ) -> None:
        """__temporal_workflow_metadata query should not produce a trace,
        but user-defined queries should still be traced.

        Uses add_temporal_runs=False on the query client to suppress
        client-side QueryWorkflow traces, isolating the test to
        worker-side HandleQuery traces only.
        """

        task_queue = f"query-filter-{uuid.uuid4()}"
        collector = InMemoryRunCollector()
        mock_ls = make_mock_ls_client(collector)

        # Worker client: add_temporal_runs=True so HandleQuery traces are created
        worker_client = _make_temporal_client(client, mock_ls, add_temporal_runs=True)
        # Query client: add_temporal_runs=False to suppress client-side traces
        query_client = _make_temporal_client(client, mock_ls, add_temporal_runs=False)

        async with new_worker(
            worker_client,
            QueryFilteringWorkflow,
            task_queue=task_queue,
            max_cached_workflows=0,
        ) as worker:
            handle = await query_client.start_workflow(
                QueryFilteringWorkflow.run,
                id=f"query-filter-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            # Wait for workflow to start by polling the user query
            assert await _poll_query(
                handle,
                QueryFilteringWorkflow.my_query,
                expected="query-result",
            ), "Workflow never started"

            collector.clear()

            # Built-in queries — should NOT be traced
            await handle.query("__temporal_workflow_metadata")
            await handle.query("__stack_trace")
            await handle.query("__enhanced_stack_trace")

            # User query — should be traced
            await handle.query(QueryFilteringWorkflow.my_query)

            await handle.signal(QueryFilteringWorkflow.complete)
            assert await handle.result() == "done"

        # Built-in queries should be absent; only user query and signal remain.
        traces = dump_traces(collector)
        assert traces == [
            ["HandleQuery:my_query"],
            ["HandleSignal:complete"],
        ], f"Unexpected traces: {traces}"
