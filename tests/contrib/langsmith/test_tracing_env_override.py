"""Tests that LangSmithPlugin defers to ``langsmith.utils.tracing_is_enabled()``.

Tracing requires the env to explicitly say so (``LANGSMITH_TRACING=true`` etc);
it is off when the env is unset or set to ``false``. Tests verify that
``LANGSMITH_TRACING=false`` produces zero runs and ``LANGSMITH_TRACING=true``
produces runs.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from langsmith import traceable

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from tests.contrib.langsmith.test_integration import (
    DirectTraceableNexusService,
    NexusDirectTraceableWorkflow,
    _make_client_and_collector,
)
from tests.helpers import new_worker
from tests.helpers.nexus import make_nexus_endpoint_name

# ---------------------------------------------------------------------------
# Sample workflow / activity
# ---------------------------------------------------------------------------


@traceable(name="inner_call")
async def _inner_call(prompt: str) -> str:
    return f"response to: {prompt}"


@traceable
@activity.defn
async def env_override_activity() -> str:
    result = await _inner_call("hello")
    return result


@workflow.defn
class EnvOverrideWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            env_override_activity,
            start_to_close_timeout=timedelta(seconds=10),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTracingEnvOverride:
    """LangSmithPlugin must respect LANGSMITH_TRACING=false."""

    async def test_no_runs_when_tracing_disabled_with_temporal_runs(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With LANGSMITH_TRACING=false and add_temporal_runs=True, no runs."""
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            EnvOverrideWorkflow,
            activities=[env_override_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                EnvOverrideWorkflow.run,
                id=f"env-override-temporal-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert result == "response to: hello"
        assert len(collector.runs) == 0, (
            f"Expected zero LangSmith runs when LANGSMITH_TRACING=false, "
            f"but got {len(collector.runs)}: "
            f"{[r.name for r in collector.runs]}"
        )

    async def test_no_runs_when_tracing_disabled_without_temporal_runs(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With LANGSMITH_TRACING=false and add_temporal_runs=False, no runs."""
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=False
        )

        async with new_worker(
            temporal_client,
            EnvOverrideWorkflow,
            activities=[env_override_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                EnvOverrideWorkflow.run,
                id=f"env-override-no-temporal-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert result == "response to: hello"
        assert len(collector.runs) == 0, (
            f"Expected zero LangSmith runs when LANGSMITH_TRACING=false, "
            f"but got {len(collector.runs)}: "
            f"{[r.name for r in collector.runs]}"
        )

    async def test_no_runs_when_langchain_tracing_v2_disabled(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LANGCHAIN_TRACING_V2=false also suppresses runs (legacy env var)."""
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            EnvOverrideWorkflow,
            activities=[env_override_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                EnvOverrideWorkflow.run,
                id=f"env-override-v2-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert result == "response to: hello"
        assert len(collector.runs) == 0, (
            f"Expected zero LangSmith runs when LANGCHAIN_TRACING_V2=false, "
            f"but got {len(collector.runs)}: "
            f"{[r.name for r in collector.runs]}"
        )

    async def test_no_runs_when_tracing_disabled_for_nexus_start(
        self,
        client: Client,
        env: WorkflowEnvironment,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With LANGSMITH_TRACING=false, nexus start handler emits no runs."""
        if env.supports_time_skipping_v1:
            pytest.skip("Time-skipping server doesn't persist headers.")

        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        task_queue = f"env-override-nexus-{uuid.uuid4()}"
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
            handle = await temporal_client.start_workflow(
                NexusDirectTraceableWorkflow.run,
                id=f"env-override-nexus-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert result == "response to: nexus-input"
        assert len(collector.runs) == 0, (
            f"Expected zero LangSmith runs when LANGSMITH_TRACING=false "
            f"(nexus start path), but got {len(collector.runs)}: "
            f"{[r.name for r in collector.runs]}"
        )

    # NOTE: test_no_runs_when_tracing_disabled_for_nexus_cancel is not
    # included — cancelling an in-flight nexus operation requires non-trivial
    # orchestration (long-running handler + external cancel signal). Flagged
    # for a follow-up ticket.

    async def test_runs_emitted_when_tracing_enabled(
        self,
        client: Client,
        env: WorkflowEnvironment,  # type:ignore[reportUnusedParameter]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Positive control: with LANGSMITH_TRACING=true, runs ARE emitted."""
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

        temporal_client, collector, _ = _make_client_and_collector(
            client, add_temporal_runs=True
        )

        async with new_worker(
            temporal_client,
            EnvOverrideWorkflow,
            activities=[env_override_activity],
            max_cached_workflows=0,
        ) as worker:
            handle = await temporal_client.start_workflow(
                EnvOverrideWorkflow.run,
                id=f"env-enabled-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()

        assert result == "response to: hello"
        assert len(collector.runs) > 0, (
            "Expected LangSmith runs when LANGSMITH_TRACING=true, but got none"
        )
