"""Recorded histories replay cleanly through the plugin.

The Deep Agents control loop runs in the workflow, so replay determinism is the
core safety property. This records the history of a real run (a plain fake agent
driven by ``run_deep_agent``, no LangChain needed) and feeds it back through a
``Replayer`` configured with the plugin. A nondeterministic seam would raise on
replay; a clean pass proves the in-workflow dispatch is deterministic.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
from temporalio import workflow
from temporalio.contrib.deepagents import DeepAgentsPlugin, run_deep_agent
from temporalio.worker import Replayer, Worker


class FakeAgent:
    async def ainvoke(self, input: Any) -> dict:
        messages = list(input.get("messages", [])) if isinstance(input, dict) else []
        return {"messages": [*messages, "answered"], "todos": []}


@workflow.defn
class ReplayWorkflow:
    @workflow.run
    async def run(self, input: dict) -> dict:
        return await run_deep_agent(FakeAgent(), input)


@pytest.mark.asyncio
async def test_replay_with_plugin(env) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-replay",
        workflows=[ReplayWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ReplayWorkflow.run,
            {"messages": ["question"]},
            id=f"da-replay-{uuid.uuid4()}",
            task_queue="da-replay",
        )
        await handle.result()
        history = await handle.fetch_history()

    # A fresh replayer (new worker identity) must replay the recorded history
    # without a nondeterminism error.
    replayer = Replayer(
        workflows=[ReplayWorkflow],
        plugins=[DeepAgentsPlugin()],
    )
    await replayer.replay_workflow(history)
