"""The tool seam: existing activities and wrapped tools route to activities.

These need LangChain (a tool is a ``BaseTool``) and guard on its import. Each
runs a real workflow that invokes the wrapped tool and asserts it scheduled the
expected activity.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("langchain_core")

from temporalio import activity, workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.deepagents import (  # noqa: E402
        DeepAgentsPlugin,
        activity_as_tool,
        tool_as_activity,
    )
    from temporalio.contrib.deepagents._tools import warn_unwrapped_tools  # noqa: E402

INVOKE_TOOL = "deepagents.invoke_tool"

# Bind deepagents symbols off the module importorskip returns: a static
# `from deepagents import ...` cannot resolve on Python 3.10 (deepagents
# needs >= 3.11), and with the package absent the type checkers mis-resolve
# the name against this same-named test directory.
create_deep_agent = pytest.importorskip("deepagents").create_deep_agent


def pairing_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"weather:{city}"


@activity.defn
async def echo_activity(text: str) -> str:
    return f"echo:{text}"


@workflow.defn
class ActivityAsToolWorkflow:
    @workflow.run
    async def run(self, text: str) -> str:
        tool = activity_as_tool(
            echo_activity, start_to_close_timeout=timedelta(seconds=10)
        )
        return await tool.ainvoke({"text": text})


@workflow.defn
class ToolAsActivityWorkflow:
    @workflow.run
    async def run(self, city: str) -> str:
        def get_weather(city: str) -> str:
            """Look up the weather for a city."""
            return f"sunny in {city}"

        tool = tool_as_activity(
            get_weather, start_to_close_timeout=timedelta(seconds=10)
        )
        # The wrapper returns plain CONTENT (the tool node stamps the model's
        # tool_call_id onto it) — not a pre-built ToolMessage.
        return str(await tool.ainvoke({"city": city}))


@pytest.mark.asyncio
async def test_activity_as_tool(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-act-tool",
        workflows=[ActivityAsToolWorkflow],
        activities=[echo_activity],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ActivityAsToolWorkflow.run,
            "hi",
            id=f"da-act-tool-{uuid.uuid4()}",
            task_queue="da-act-tool",
        )
        out = await handle.result()

    assert out == "echo:hi"
    counts = await count_scheduled_activities(handle)
    assert counts["echo_activity"] == 1, counts


@pytest.mark.asyncio
async def test_tool_as_activity(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-tool-act",
        workflows=[ToolAsActivityWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ToolAsActivityWorkflow.run,
            "Paris",
            id=f"da-tool-act-{uuid.uuid4()}",
            task_queue="da-tool-act",
        )
        out = await handle.result()

    assert "sunny in Paris" in out
    counts = await count_scheduled_activities(handle)
    assert counts[INVOKE_TOOL] == 1, counts


def test_builtin_tool_in_workflow(recwarn: pytest.WarningsRecorder) -> None:
    # Built-in tool names never warn (they are pure, in-workflow); an unwrapped
    # user tool does warn so the Workflow-vs-Activity choice is conscious.
    warn_unwrapped_tools([SimpleNamespace(name="write_todos")])
    assert len(recwarn) == 0

    warn_unwrapped_tools([SimpleNamespace(name="scrape_website")])
    assert any("scrape_website" in str(w.message) for w in recwarn)


# Turn-2 requests observed by the fake model, captured activity-side. Module
# state is shared with the in-process worker, same as the tool registries.
_captured_requests: list[list] = []


@workflow.defn
class ToolCallIdPairingWorkflow:
    @workflow.run
    async def run(self, city: str) -> str:
        weather_tool = tool_as_activity(
            pairing_weather, start_to_close_timeout=timedelta(seconds=10)
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            tools=[weather_tool],
            system_prompt="Use the weather tool.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": f"Weather in {city}?"}]}
        )
        return str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_wrapped_tool_result_pairs_with_model_tool_call_id(
    env: WorkflowEnvironment,
) -> None:
    """The tool_result the model sees on turn 2 must carry the model's OWN
    tool_call_id. Regression: ``tool_as_activity`` returned the activity-built
    ``ToolMessage`` whose workflow-generated id a real provider (Anthropic)
    rejects as an unpaired ``tool_result`` — offline fakes never validate the
    pairing, so only a live-model run surfaced it. The wrapper now returns
    plain content and the tool node stamps the correct id.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    from temporalio.contrib.deepagents.testing import FakeModel

    _captured_requests.clear()

    class RecordingModel(FakeModel):
        def __init__(self, responses: Sequence[Any]) -> None:
            super().__init__(responses)

        async def _agenerate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            _captured_requests.append(list(messages))
            return await super()._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    tool_turn = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "pairing_weather",
                "args": {"city": "Paris"},
                "id": "toolu_scripted_pairing_id",
            }
        ],
    )
    final = AIMessage(content="It is sunny in Paris.")
    responses = [tool_turn, final]
    cursor = {"i": 0}

    def provider(_model_name: str) -> RecordingModel:
        reply = responses[cursor["i"] % len(responses)]
        cursor["i"] += 1
        return RecordingModel([reply])

    plugin = DeepAgentsPlugin(model_provider=provider)
    async with Worker(
        env.client,
        task_queue="da-toolcall-pairing",
        workflows=[ToolCallIdPairingWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            ToolCallIdPairingWorkflow.run,
            "Paris",
            id=f"da-toolcall-pairing-{uuid.uuid4()}",
            task_queue="da-toolcall-pairing",
        )
        out = await handle.result()

    assert "sunny" in out.lower()
    # Turn 2's request must contain the tool result under the MODEL's id.
    assert len(_captured_requests) >= 2, len(_captured_requests)
    tool_messages = [m for m in _captured_requests[1] if isinstance(m, ToolMessage)]
    assert tool_messages, _captured_requests[1]
    assert tool_messages[0].tool_call_id == "toolu_scripted_pairing_id", tool_messages[
        0
    ].tool_call_id
    assert "weather:Paris" in str(tool_messages[0].content)
