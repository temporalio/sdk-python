"""Tests for LangSmithPlugin construction, configuration, and end-to-end usage."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from langsmith import traceable, tracing_context

from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.langsmith import LangSmithInterceptor, LangSmithPlugin
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.conftest import dump_traces, find_traces
from tests.contrib.langsmith.test_integration import (
    ComprehensiveWorkflow,
    NexusService,
    TraceableActivityWorkflow,
    _make_client_and_collector,
    _poll_query,
    nested_traceable_activity,
    traceable_activity,
)
from tests.helpers import new_worker
from tests.helpers.nexus import make_nexus_endpoint_name


class TestPluginConstruction:
    """Tests for LangSmithPlugin construction."""

    def test_construction_stores_all_config(self) -> None:
        """All constructor kwargs are stored on the interceptor."""
        mock_client = MagicMock()
        plugin = LangSmithPlugin(
            client=mock_client,
            project_name="my-project",
            add_temporal_runs=False,
            default_metadata={"env": "prod"},
            default_tags=["v1"],
        )
        assert plugin.interceptors is not None
        assert len(plugin.interceptors) > 0
        interceptor = plugin.interceptors[0]
        assert isinstance(interceptor, LangSmithInterceptor)
        assert interceptor._client is mock_client
        assert interceptor._project_name == "my-project"
        assert interceptor._add_temporal_runs is False
        assert interceptor._default_metadata == {"env": "prod"}
        assert interceptor._default_tags == ["v1"]


class TestPluginIntegration:
    """End-to-end test using LangSmithPlugin as a Temporal client plugin."""

    async def test_comprehensive_plugin_trace_hierarchy(
        self, client: Client, env: WorkflowEnvironment
    ) -> None:
        """Plugin wired to a real Temporal worker produces the full trace hierarchy.

        user_pipeline only wraps start_workflow, so poll/query/signal/update
        traces are naturally separate root traces.
        """
        if env.supports_time_skipping:
            pytest.skip("Time-skipping server doesn't persist headers.")

        temporal_client, collector, mock_ls_client = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        task_queue = f"plugin-comprehensive-{uuid.uuid4()}"
        workflow_id = f"plugin-comprehensive-{uuid.uuid4()}"

        @traceable(name="user_pipeline")
        async def user_pipeline() -> WorkflowHandle[Any, Any]:
            return await temporal_client.start_workflow(
                ComprehensiveWorkflow.run,
                id=workflow_id,
                task_queue=task_queue,
            )

        with tracing_context(client=mock_ls_client, enabled=True):
            handle = await user_pipeline()

            async with new_worker(
                temporal_client,
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
                await handle.query(ComprehensiveWorkflow.my_query)
                await handle.signal(ComprehensiveWorkflow.my_signal, "hello")
                await handle.execute_update(
                    ComprehensiveWorkflow.my_unvalidated_update, "test"
                )
                await handle.execute_update(ComprehensiveWorkflow.my_update, "finish")
                result = await handle.result()

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

        # Each remaining operation is its own root trace
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
