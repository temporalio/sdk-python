"""Continue-as-new state carry for long-running Deep Agents.

``run_deep_agent(continue_as_new_after=...)`` keeps a long conversation from
bloating workflow history: once the current turn finishes past the threshold and
there is still pending work, it snapshots the accumulated messages plus the
model/tool result cache and continues into a fresh run. These tests use a plain
fake agent (no LangChain needed) so they boot a real Temporal server and exercise
the continue-as-new machinery end to end.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
from temporalio import workflow
from temporalio.contrib.deepagents import DeepAgentsPlugin, _serde, run_deep_agent
from temporalio.worker import Worker


class FakeAgent:
    """A stand-in compiled agent that appends a step and reports a todo.

    It is *not* a LangChain object — it just satisfies the ``ainvoke`` shape
    ``run_deep_agent`` drives, so the continue-as-new path can be tested without
    a model provider or the LangChain import tree.
    """

    async def ainvoke(self, input: Any) -> dict:
        messages = list(input.get("messages", [])) if isinstance(input, dict) else []
        messages = [*messages, "step"]
        done = messages.count("step") >= 3
        return {
            "messages": messages,
            "todos": [
                {"content": "work", "status": "completed" if done else "pending"}
            ],
        }


@workflow.defn
class ContinueAsNewWorkflow:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> dict:
        # Threshold of 1 means: continue-as-new as soon as there is pending work,
        # which the fake agent reports until it has appended 3 steps.
        return await run_deep_agent(
            FakeAgent(),
            input,
            continue_as_new_after=1,
            state_snapshot=state_snapshot,
        )


@pytest.mark.asyncio
async def test_can_threshold_and_cache(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-can",
        workflows=[ContinueAsNewWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ContinueAsNewWorkflow.run,
            {"messages": ["start"]},
            id=f"da-can-{uuid.uuid4()}",
            task_queue="da-can",
        )
        result = await handle.result()

    assert result["messages"] == ["start", "step", "step", "step"], result
    assert result["todos"][0]["status"] == "completed"


def test_state_snapshot_roundtrip() -> None:
    # The result cache carried in a snapshot rehydrates to the same hits, so work
    # done before a continue-as-new is reused, not recomputed, afterwards.
    _serde.set_result_cache({})
    key = _serde.cache_key("model", "fake:model", [["m"], []])
    _serde.cache_put(key, {"dumped": "message"})
    snapshot = _serde.result_cache_snapshot()
    assert snapshot and key in snapshot

    # Simulate the continued run: a fresh cache seeded from the snapshot.
    _serde.set_result_cache(dict(snapshot))
    hit, value = _serde.cache_lookup(key)
    assert hit and value == {"dumped": "message"}


class SlowFakeAgent:
    """Like ``FakeAgent`` but each turn burns timers so a single run's history
    grows past the dev server's continue-as-new suggestion threshold (the test
    env pins ``limit.historyCount.suggestContinueAsNew`` low)."""

    async def ainvoke(self, input: Any) -> dict:
        for _ in range(20):
            await workflow.sleep(0.001)
        messages = list(input.get("messages", [])) if isinstance(input, dict) else []
        messages = [*messages, "step"]
        done = len(messages) >= 3
        return {
            "messages": messages,
            "todos": [
                {"content": "work", "status": "completed" if done else "pending"}
            ],
        }


@workflow.defn
class SuggestedCanWorkflow:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> dict:
        # No continue_as_new_after: the default follows the server's own
        # is_continue_as_new_suggested() signal.
        return await run_deep_agent(
            SlowFakeAgent(),
            input,
            state_snapshot=state_snapshot,
        )


@pytest.mark.asyncio
async def test_can_defaults_to_server_suggestion(
    env: WorkflowEnvironment, env_type: str
) -> None:
    """With ``continue_as_new_after`` unset, the driver continues-as-new when
    the SERVER suggests it (history count/size), not on a fixed threshold."""
    if env_type != "local":
        pytest.skip("needs the local dev server's low suggestContinueAsNew threshold")
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-can-suggested",
        workflows=[SuggestedCanWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            SuggestedCanWorkflow.run,
            {"messages": ["start"]},
            id=f"da-can-suggested-{uuid.uuid4()}",
            task_queue="da-can-suggested",
        )
        result = await handle.result()

    # Carry across the suggested continue-as-new: the conversation only reaches
    # 3 messages if snapshots crossed run boundaries.
    assert result["messages"] == ["start", "step", "step"], result
    assert result["todos"][0]["status"] == "completed"
    # The first run really did continue-as-new (not complete).
    first = env.client.get_workflow_handle(
        handle.id, run_id=handle.first_execution_run_id
    )
    desc = await first.describe()
    assert desc.status is not None and desc.status.name == "CONTINUED_AS_NEW", (
        desc.status
    )
