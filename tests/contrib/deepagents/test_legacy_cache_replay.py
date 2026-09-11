"""The retired legacy cache still replays pre-change histories.

The checked-in history was recorded before this change: two model-issued tool
calls with identical name+args, deduped by the legacy result cache to ONE
scheduled ``deepagents.invoke_tool`` activity. Its replay exercises the
unpatched branch of ``deepagents.retire-result-cache`` end to end — the only
executable coverage that branch has, and the tripwire that keeps it from
being "simplified" away while pre-change workflows are still in flight.

Fixture provenance: recorded on sdk-python main (pre-#1806) by driving a
scripted two-identical-tool-call agent through ``run_deep_agent`` on a local
dev server. Delete this test and the fixture together when the patch id goes
through ``workflow.deprecate_patch``.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.contrib.deepagents import (
    DeepAgentsPlugin,
    run_deep_agent,
    tool_as_activity,
)
from temporalio.contrib.deepagents.testing import mock_model_provider
from temporalio.worker import Replayer

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

_HISTORY = Path(__file__).parent / "histories" / "legacy_dedup_repeated_tool_calls.json"


@tool
def pairing_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"weather:{city}"


@workflow.defn
class RepeatedToolCallWorkflow:
    @workflow.run
    async def run(self) -> str:
        t = tool_as_activity(
            pairing_weather, start_to_close_timeout=timedelta(seconds=10)
        )
        agent = create_deep_agent(model="fake:model", tools=[t])
        result = await run_deep_agent(
            agent,
            {"messages": [{"role": "user", "content": "Check Paris twice."}]},
            continue_as_new_after=10_000,
        )
        return str(result["messages"][-1].content)


RESPONSES: list[Any] = [
    AIMessage(
        content="",
        tool_calls=[
            {"name": "pairing_weather", "args": {"city": "Paris"}, "id": "first-call"}
        ],
    ),
    AIMessage(
        content="",
        tool_calls=[
            {"name": "pairing_weather", "args": {"city": "Paris"}, "id": "second-call"}
        ],
    ),
    AIMessage(content="done"),
]


@pytest.mark.asyncio
async def test_legacy_dedup_history_replays() -> None:
    plugin = DeepAgentsPlugin(model_provider=mock_model_provider(RESPONSES))
    replayer = Replayer(workflows=[RepeatedToolCallWorkflow], plugins=[plugin])
    history = WorkflowHistory.from_json("legacy-replay", _HISTORY.read_text())
    await replayer.replay_workflow(history)
