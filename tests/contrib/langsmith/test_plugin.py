"""Tests for LangSmithPlugin construction, configuration, and end-to-end usage."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from langsmith import traceable, tracing_context

from temporalio.client import Client
from temporalio.contrib.langsmith import LangSmithInterceptor, LangSmithPlugin
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.conftest import dump_runs
from tests.contrib.langsmith.test_integration import (
    ComprehensiveWorkflow,
    NexusService,
    SimpleNexusWorkflow,
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
            metadata={"env": "prod"},
            tags=["v1"],
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
        """Plugin wired to a real Temporal worker produces the full trace hierarchy."""
        if env.supports_time_skipping:
            pytest.skip("Time-skipping server doesn't persist headers.")

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
                max_cached_workflows=0,
            ) as worker:
                await env.create_nexus_endpoint(
                    make_nexus_endpoint_name(worker.task_queue),
                    worker.task_queue,
                )
                workflow_id = f"plugin-comprehensive-{uuid.uuid4()}"
                handle = await temporal_client.start_workflow(
                    ComprehensiveWorkflow.run,
                    id=workflow_id,
                    task_queue=worker.task_queue,
                )
                # Poll via raw client to avoid creating trace runs
                raw_handle = client.get_workflow_handle(workflow_id)
                assert await _poll_query(
                    raw_handle,
                    ComprehensiveWorkflow.is_waiting_for_signal,
                    expected=True,
                ), "Workflow never reached signal wait point"
                await handle.query(ComprehensiveWorkflow.my_query)
                await handle.signal(ComprehensiveWorkflow.my_signal, "hello")
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
            "      outer_chain",
            "        inner_llm_call",
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
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          outer_chain",
            "            inner_llm_call",
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
