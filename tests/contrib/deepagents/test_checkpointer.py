"""Checkpointing follows the ``contrib.langgraph`` precedent.

The zero-config default is an in-workflow ``InMemorySaver`` rehydrated by
deterministic replay; no bespoke checkpointer adapter ships. A user-supplied
durable checkpointer would run its own I/O from workflow code, which is not
replay-safe, so the plugin warns (respecting the choice rather than hard-failing)
and points at the snapshot + continue-as-new path.
"""

from __future__ import annotations

import sys
import uuid
import warnings

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow
from temporalio.contrib.deepagents.workflow import warn_durable_checkpointer

# Bind deepagents symbols off the module importorskip returns: a static
# `from deepagents import ...` cannot resolve on Python 3.10 (deepagents
# needs >= 3.11), and with the package absent the type checkers mis-resolve
# the name against this same-named test directory.
create_deep_agent = pytest.importorskip("deepagents").create_deep_agent


class _DurableSaver:
    """Stand-in for a checkpointer that does its own database I/O."""


class InMemorySaver:
    """Same class name LangGraph's in-workflow saver uses."""


@workflow.defn
class CheckpointWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = create_deep_agent(model="fake:model")
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
        return str(result["messages"][-1].content)


def test_durable_checkpointer_warns() -> None:
    # None and in-workflow savers are silent...
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        warn_durable_checkpointer(None)
        warn_durable_checkpointer(InMemorySaver())
    assert not records, [str(r.message) for r in records]

    # ...a durable saver warns (respect the choice, do not hard-fail).
    with pytest.warns(UserWarning, match="durable checkpointer"):
        warn_durable_checkpointer(_DurableSaver())


@pytest.mark.asyncio
async def test_default_saver_rehydrates(env: WorkflowEnvironment) -> None:
    # Against the real deepagents default (in-workflow InMemorySaver): the agent
    # runs and its recorded history replays cleanly, proving replay rehydrates
    # the in-workflow checkpoint state with no external checkpointer.
    pytest.importorskip("deepagents")
    pytest.importorskip("langchain_core")

    from temporalio.contrib.deepagents import DeepAgentsPlugin
    from temporalio.contrib.deepagents.testing import mock_model_provider
    from temporalio.worker import Replayer, Worker

    plugin = DeepAgentsPlugin(model_provider=mock_model_provider(["Checkpointed."]))
    async with Worker(
        env.client,
        task_queue="da-ckpt",
        workflows=[CheckpointWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            CheckpointWorkflow.run,
            "hello",
            id=f"da-ckpt-{uuid.uuid4()}",
            task_queue="da-ckpt",
        )
        await handle.result()
        history = await handle.fetch_history()

    await Replayer(
        workflows=[CheckpointWorkflow], plugins=[DeepAgentsPlugin()]
    ).replay_workflow(history)
