"""Tests for LangSmithPlugin construction, configuration, and end-to-end usage."""

from __future__ import annotations

import uuid
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langsmith import traceable, tracing_context

import temporalio.worker
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.langsmith import LangSmithInterceptor, LangSmithPlugin
from temporalio.contrib.langsmith._plugin import _ClientOnlyLangSmithInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from tests.contrib.langsmith.conftest import dump_traces
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
        wrapper = plugin.interceptors[0]
        assert isinstance(wrapper, _ClientOnlyLangSmithInterceptor)
        interceptor = wrapper._interceptor
        assert isinstance(interceptor, LangSmithInterceptor)
        assert interceptor._client is mock_client
        assert interceptor._project_name == "my-project"
        assert interceptor._add_temporal_runs is False
        assert interceptor._default_metadata == {"env": "prod"}
        assert interceptor._default_tags == ["v1"]

    def test_construction_without_client(self) -> None:
        """Plugin creates a LangSmith client when none is provided."""
        plugin = LangSmithPlugin()
        assert plugin.interceptors is not None
        wrapper = plugin.interceptors[0]
        assert isinstance(wrapper, _ClientOnlyLangSmithInterceptor)
        assert wrapper._interceptor._client is not None

    def test_configure_worker_creates_fresh_interceptor(self) -> None:
        """Each configure_worker call produces a distinct LangSmithInterceptor."""
        mock_client = MagicMock()
        plugin = LangSmithPlugin(
            client=mock_client,
            project_name="test-project",
            add_temporal_runs=True,
            metadata={"k": "v"},
            tags=["t1"],
        )

        # Build a minimal worker config — super().configure_worker needs
        # config["client"].config(active_config=True) and a workflow_runner
        mock_temporal_client = MagicMock()
        mock_temporal_client.config.return_value = {}
        base_config: dict[str, Any] = {
            "client": mock_temporal_client,
            "workflow_runner": SandboxedWorkflowRunner(),
        }

        config1 = plugin.configure_worker(cast(Any, dict(base_config)))
        config2 = plugin.configure_worker(cast(Any, dict(base_config)))

        interceptors1 = [
            i
            for i in config1.get("interceptors", [])
            if isinstance(i, LangSmithInterceptor)
        ]
        interceptors2 = [
            i
            for i in config2.get("interceptors", [])
            if isinstance(i, LangSmithInterceptor)
        ]
        assert len(interceptors1) == 1
        assert len(interceptors2) == 1
        assert interceptors1[0] is not interceptors2[0]

        # Verify the fresh interceptor has the correct config
        fresh = interceptors1[0]
        assert fresh._client is mock_client
        assert fresh._project_name == "test-project"
        assert fresh._add_temporal_runs is True
        assert fresh._default_metadata == {"k": "v"}
        assert fresh._default_tags == ["t1"]

    def test_wrapper_not_worker_interceptor(self) -> None:
        """The client-only wrapper must not be a worker.Interceptor."""
        mock_client = MagicMock()
        plugin = LangSmithPlugin(client=mock_client)
        assert plugin.interceptors is not None
        wrapper = plugin.interceptors[0]
        assert isinstance(wrapper, _ClientOnlyLangSmithInterceptor)
        assert not isinstance(wrapper, temporalio.worker.Interceptor)

    def test_no_duplicate_interceptors_after_merge(self) -> None:
        """Simulating the full Worker merge flow yields exactly one LangSmithInterceptor."""
        mock_client = MagicMock()
        plugin = LangSmithPlugin(client=mock_client, add_temporal_runs=True)

        # Step 1: configure_client — adds the wrapper to client interceptors
        client_config = plugin.configure_client(cast(Any, {"interceptors": []}))

        # Step 2: build worker config where config["client"].config(active_config=True)
        # returns the client config (so the wrapper is in the client's interceptors)
        mock_temporal_client = MagicMock()
        mock_temporal_client.config.return_value = client_config

        # Step 3: configure_worker — adds a fresh LangSmithInterceptor
        worker_config = plugin.configure_worker(
            cast(
                Any,
                {
                    "client": mock_temporal_client,
                    "interceptors": [],
                    "workflow_runner": SandboxedWorkflowRunner(),
                },
            )
        )

        # Step 4: simulate _init_from_config merge (worker/_worker.py:435-441)
        client_interceptors = client_config.get("interceptors", [])
        interceptors_from_client = [
            i
            for i in client_interceptors
            if isinstance(i, temporalio.worker.Interceptor)
        ]
        assert (
            len(interceptors_from_client) == 0
        ), "Wrapper should not pass worker.Interceptor filter"
        final_interceptors = interceptors_from_client + list(
            worker_config.get("interceptors", [])
        )

        # The wrapper should NOT pass the isinstance filter, so only the fresh
        # one from configure_worker should be present
        langsmith_interceptors = [
            i for i in final_interceptors if isinstance(i, LangSmithInterceptor)
        ]
        assert len(langsmith_interceptors) == 1


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
                SimpleNexusWorkflow,
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
                await handle.execute_update(ComprehensiveWorkflow.my_update, "finish")
                result = await handle.result()

        assert result == "comprehensive-done"

        traces = dump_traces(collector)

        # user_pipeline trace: StartWorkflow + full workflow execution tree
        workflow_traces = [t for t in traces if t[0] == "user_pipeline"]
        assert len(workflow_traces) == 1
        assert workflow_traces[0] == [
            "user_pipeline",
            "  StartWorkflow:ComprehensiveWorkflow",
            "    RunWorkflow:ComprehensiveWorkflow",
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          nested_traceable_activity",
            "            outer_chain",
            "              inner_llm_call",
            # step-wrapped activity
            "      step_with_activity",
            "        StartActivity:nested_traceable_activity",
            "          RunActivity:nested_traceable_activity",
            "            nested_traceable_activity",
            "              outer_chain",
            "                inner_llm_call",
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          nested_traceable_activity",
            "            outer_chain",
            "              inner_llm_call",
            "      outer_chain",
            "        inner_llm_call",
            "      StartChildWorkflow:TraceableActivityWorkflow",
            "        RunWorkflow:TraceableActivityWorkflow",
            "          StartActivity:traceable_activity",
            "            RunActivity:traceable_activity",
            "              traceable_activity",
            "                inner_llm_call",
            # step-wrapped child workflow
            "      step_with_child_workflow",
            "        StartChildWorkflow:TraceableActivityWorkflow",
            "          RunWorkflow:TraceableActivityWorkflow",
            "            StartActivity:traceable_activity",
            "              RunActivity:traceable_activity",
            "                traceable_activity",
            "                  inner_llm_call",
            "      StartNexusOperation:NexusService/run_operation",
            "        RunStartNexusOperationHandler:NexusService/run_operation",
            "          StartWorkflow:SimpleNexusWorkflow",
            "            RunWorkflow:SimpleNexusWorkflow",
            "              StartActivity:traceable_activity",
            "                RunActivity:traceable_activity",
            "                  traceable_activity",
            "                    inner_llm_call",
            # step-wrapped nexus operation
            "      step_with_nexus",
            "        StartNexusOperation:NexusService/run_operation",
            "          RunStartNexusOperationHandler:NexusService/run_operation",
            "            StartWorkflow:SimpleNexusWorkflow",
            "              RunWorkflow:SimpleNexusWorkflow",
            "                StartActivity:traceable_activity",
            "                  RunActivity:traceable_activity",
            "                    traceable_activity",
            "                      inner_llm_call",
            # post-signal
            "      StartActivity:nested_traceable_activity",
            "        RunActivity:nested_traceable_activity",
            "          nested_traceable_activity",
            "            outer_chain",
            "              inner_llm_call",
        ]

        # poll_query trace (separate root, variable number of iterations)
        poll_traces = [t for t in traces if t[0] == "poll_query"]
        assert len(poll_traces) == 1
        poll = poll_traces[0]
        assert poll[0] == "poll_query"
        poll_children = poll[1:]
        for i in range(0, len(poll_children), 2):
            assert poll_children[i] == "  QueryWorkflow:is_waiting_for_signal"
            assert poll_children[i + 1] == "    HandleQuery:is_waiting_for_signal"

        # Each remaining operation is its own root trace
        query_traces = [t for t in traces if t[0] == "QueryWorkflow:my_query"]
        assert len(query_traces) == 1
        assert query_traces[0] == [
            "QueryWorkflow:my_query",
            "  HandleQuery:my_query",
        ]

        signal_traces = [t for t in traces if t[0] == "SignalWorkflow:my_signal"]
        assert len(signal_traces) == 1
        assert signal_traces[0] == [
            "SignalWorkflow:my_signal",
            "  HandleSignal:my_signal",
        ]

        update_traces = [t for t in traces if t[0] == "StartWorkflowUpdate:my_update"]
        assert len(update_traces) == 1
        assert update_traces[0] == [
            "StartWorkflowUpdate:my_update",
            "  ValidateUpdate:my_update",
            "  HandleUpdate:my_update",
        ]
