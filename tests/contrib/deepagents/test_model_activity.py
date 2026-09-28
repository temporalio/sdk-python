"""The model seam: every ``TemporalModel`` generation is one activity.

These exercise the seam directly through ``TemporalModel`` (no full agent
needed), so they depend only on LangChain, not on deepagents.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("langchain_core")

from temporalio import workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import HumanMessage

    from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalModel
    from temporalio.contrib.deepagents.testing import mock_model_provider

INVOKE_MODEL = "deepagents.invoke_model"


@workflow.defn
class ModelWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel(model="fake:model")
        message = await model.ainvoke([HumanMessage(content=prompt)])
        return str(message.content)


@workflow.defn
class ExplicitTimeoutWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel(
            model="fake:model",
            activity_options={"start_to_close_timeout": timedelta(seconds=20)},
        )
        message = await model.ainvoke([HumanMessage(content=prompt)])
        return str(message.content)


@pytest.mark.asyncio
async def test_model_call_is_activity(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["The capital of France is Paris."]),
        model_activity_options={"start_to_close_timeout": timedelta(seconds=30)},
    )
    async with Worker(
        env.client,
        task_queue="da-model",
        workflows=[ModelWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ModelWorkflow.run,
            "What is the capital of France?",
            id=f"da-model-{uuid.uuid4()}",
            task_queue="da-model",
        )
        out = await handle.result()
    assert "Paris" in out
    counts = await count_scheduled_activities(handle)
    assert counts[INVOKE_MODEL] == 1, counts


@pytest.mark.asyncio
async def test_temporal_model_explicit(env: WorkflowEnvironment) -> None:
    # The explicit escape hatch routes through the same activity, with a
    # per-model timeout override rather than the plugin default.
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["Bonjour."]),
    )
    async with Worker(
        env.client,
        task_queue="da-model-explicit",
        workflows=[ExplicitTimeoutWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ExplicitTimeoutWorkflow.run,
            "hi",
            id=f"da-model-x-{uuid.uuid4()}",
            task_queue="da-model-explicit",
        )
        out = await handle.result()
    assert out == "Bonjour."
    counts = await count_scheduled_activities(handle)
    assert counts[INVOKE_MODEL] == 1, counts


class _ResampleAgent:
    """ainvoke-shaped driver: the SAME prompt sent twice through a
    TemporalModel — deliberate resampling — under run_deep_agent so the
    continue-as-new result cache is active."""

    def __init__(self) -> None:
        self.model = TemporalModel(model="fake:model")

    async def ainvoke(self, _input: Any) -> dict:
        first = await self.model.ainvoke([HumanMessage(content="same prompt")])
        second = await self.model.ainvoke([HumanMessage(content="same prompt")])
        return {
            "messages": [f"{first.content}|{second.content}"],
            "todos": [],
        }


@workflow.defn
class ResampleWorkflow:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> str:
        from temporalio.contrib.deepagents import run_deep_agent

        result = await run_deep_agent(
            _ResampleAgent(), input, state_snapshot=state_snapshot
        )
        return str(result["messages"][-1])


@pytest.mark.asyncio
async def test_repeated_identical_model_call_resamples(
    env: WorkflowEnvironment,
) -> None:
    """Two identical live model calls each run their own Activity and can
    return different responses (deliberate resampling) — under the active
    CAN cache, the old input-only key served the FIRST response twice."""
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["first answer", "second answer"]),
    )
    async with Worker(
        env.client,
        task_queue="da-model-resample",
        workflows=[ResampleWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            ResampleWorkflow.run,
            {"messages": []},
            id=f"da-model-resample-{uuid.uuid4()}",
            task_queue="da-model-resample",
        )
        out = await handle.result()

    assert out == "first answer|second answer", out
    counts = await count_scheduled_activities(handle)
    assert counts["deepagents.invoke_model"] == 2, counts
