"""Human-in-the-loop: SDK-native interrupt mapped to Query + Update.

``interrupt_on={...}`` makes Deep Agents pause before a guarded tool runs. With a
checkpointer configured, LangGraph does *not* raise out of ``ainvoke`` — it
returns the current state with an ``__interrupt__`` entry describing the pending
approval (verified against deepagents 0.6.12 / langchain 1.x). Because the loop
runs in the workflow, that pause surfaces directly in workflow code. The plugin's
recommended mapping — used here — is: detect the returned ``__interrupt__``,
expose its payload via a Query, and resume with a Workflow Update carrying the
human's decision through ``Command(resume=...)``. No shim exception is invented;
the native LangGraph resume protocol (``{"decisions": [{"type": ...}]}``) is used
as-is.

State lives on the workflow instance (per-execution), which is the idiomatic
Temporal pattern for state shared between the run method and its handlers.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from temporalio import workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from temporalio.contrib.deepagents import DeepAgentsPlugin, tool_as_activity
    from temporalio.contrib.deepagents.testing import mock_model_provider


@workflow.defn
class HitlWorkflow:
    def __init__(self) -> None:
        self._interrupt: str | None = None
        self._resume_value: str | None = None
        self._resumed = False

    @workflow.run
    async def run(self, city: str) -> str:
        def book_trip(city: str) -> str:
            """Book a trip to a city (requires human approval)."""
            return f"Booked a trip to {city}."

        trip_tool = tool_as_activity(
            book_trip, start_to_close_timeout=timedelta(seconds=30)
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            tools=[trip_tool],
            interrupt_on={"book_trip": True},
            checkpointer=InMemorySaver(),
        )
        config: RunnableConfig = {
            "configurable": {"thread_id": workflow.info().workflow_id}
        }
        payload: Any = {"messages": [{"role": "user", "content": f"Book {city}."}]}
        result = await agent.ainvoke(payload, config=config)
        # LangGraph returns (not raises) the pending approval under __interrupt__.
        pending = result.get("__interrupt__")
        if pending:
            self._interrupt = str(getattr(pending[0], "value", pending[0]))
            await workflow.wait_condition(lambda: self._resumed)
            result = await agent.ainvoke(
                Command(resume={"decisions": [{"type": self._resume_value}]}),
                config=config,
            )
        return result["messages"][-1].content

    @workflow.query
    def pending_interrupt(self) -> str | None:
        return self._interrupt

    @workflow.update
    async def resume(self, decision: str) -> None:
        self._resume_value = decision
        self._resumed = True


@pytest.mark.asyncio
async def test_interrupt_query_then_resume(env) -> None:
    approve = AIMessage(
        content="",
        tool_calls=[{"name": "book_trip", "args": {"city": "Rome"}, "id": "c1"}],
    )
    done = AIMessage(content="Booked a trip to Rome.")
    plugin = DeepAgentsPlugin(model_provider=mock_model_provider([approve, done]))
    async with Worker(
        env.client,
        task_queue="da-hitl",
        workflows=[HitlWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            HitlWorkflow.run,
            "Rome",
            id=f"da-hitl-{uuid.uuid4()}",
            task_queue="da-hitl",
        )

        # Wait for the agent to hit the interrupt (surfaced via the Query), then
        # approve via an Update. A bounded poll with a sleep, so a regression that
        # never raises the interrupt fails fast instead of busy-spinning.
        for _ in range(100):
            if await handle.query(HitlWorkflow.pending_interrupt) is not None:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("workflow never surfaced the HITL interrupt via the query")

        await handle.execute_update(HitlWorkflow.resume, "approve")
        out = await handle.result()

    assert "Rome" in out
