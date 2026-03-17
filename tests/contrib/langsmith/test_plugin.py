"""Tests for LangSmithPlugin construction, configuration, and end-to-end usage."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from langsmith import traceable, tracing_context

from temporalio.client import Client
from temporalio.contrib.langsmith import LangSmithInterceptor, LangSmithPlugin
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.conftest import dump_runs
from tests.contrib.langsmith.test_integration import (
    ComprehensiveWorkflow,
    TraceableActivityWorkflow,
    _make_client_and_collector,
    nested_traceable_activity,
    traceable_activity,
)
from tests.helpers import new_worker


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
        temporal_client, collector, mock_ls_client = _make_client_and_collector(client)

        @traceable(name="user_pipeline")
        async def user_pipeline() -> str:
            async with new_worker(
                temporal_client,
                ComprehensiveWorkflow,
                TraceableActivityWorkflow,
                activities=[nested_traceable_activity, traceable_activity],
            ) as worker:
                handle = await temporal_client.start_workflow(
                    ComprehensiveWorkflow.run,
                    id=f"plugin-comprehensive-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                # Query
                await handle.query(ComprehensiveWorkflow.my_query)
                # Signal
                await handle.signal(ComprehensiveWorkflow.my_signal, "hello")
                # Update (completes the workflow)
                await handle.execute_update(
                    ComprehensiveWorkflow.my_update, "finish"
                )
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
            "  QueryWorkflow:my_query",
            "    HandleQuery:my_query",
            "  SignalWorkflow:my_signal",
            "    HandleSignal:my_signal",
            "  StartWorkflowUpdate:my_update",
            "    ValidateUpdate:my_update",
            "    HandleUpdate:my_update",
        ]
        assert hierarchy == expected, (
            f"Hierarchy mismatch.\nExpected:\n{expected}\nActual:\n{hierarchy}"
        )
