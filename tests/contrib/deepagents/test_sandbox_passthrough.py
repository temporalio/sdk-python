"""The plugin's sandbox passthrough makes explicit import guards unnecessary.

This module imports ``deepagents`` at the top WITHOUT
``workflow.unsafe.imports_passed_through()``. The workflow sandbox re-imports
the defining module of every workflow, so if the plugin's passthrough
configuration did not cover deepagents' import tree, worker registration
below would fail. This is the executable proof behind the README's
guard-free examples.

Also the e2e for ``create_temporal_deep_agent``: the explicit construction
path that scopes ``activity_options`` to one agent.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

import deepagents  # noqa: F401  # pyright: ignore[reportUnusedImport, reportImplicitRelativeImport]

from temporalio import workflow
from temporalio.contrib.deepagents import (
    DeepAgentsPlugin,
    create_temporal_deep_agent,
)
from temporalio.worker import Worker
from tests.contrib.deepagents.helpers import count_scheduled_activities


@workflow.defn
class GuardFreeAgentWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = create_temporal_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            activity_options={"start_to_close_timeout": timedelta(minutes=2)},
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        return str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_guard_free_import_and_explicit_agent(
    env: WorkflowEnvironment,
) -> None:
    from temporalio.contrib.deepagents.testing import mock_model_provider

    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["The answer is 42."]),
    )
    async with Worker(
        env.client,
        task_queue="da-guard-free",
        workflows=[GuardFreeAgentWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            GuardFreeAgentWorkflow.run,
            "What is the meaning of life?",
            id=f"da-guard-free-{uuid.uuid4()}",
            task_queue="da-guard-free",
        )
        out = await handle.result()

    assert "42" in out
    counts = await count_scheduled_activities(handle)
    assert counts["deepagents.invoke_model"] == 1, counts


def test_wrapper_rejects_options_without_wrappable_model() -> None:
    with pytest.raises(ValueError, match="activity_options requires"):
        create_temporal_deep_agent(
            model=object(),
            activity_options={"start_to_close_timeout": timedelta(minutes=1)},
        )
