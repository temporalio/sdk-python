"""Cardinal end-to-end test: unmodified ``deepagents`` code, made durable.

Builds a real Deep Agent with ``create_deep_agent(...)`` and drives it with
``agent.ainvoke(...)`` inside a ``@workflow.defn`` — the only addition is
``plugins=[DeepAgentsPlugin(...)]``. No user call to
``workflow.execute_activity``; the plugin routes model and tool calls to
activities under the hood.

Guards on the ``deepagents`` / ``langchain_core`` imports (capability detection),
because they are the plugin's own runtime dependency and may be absent on a
docs-only checkout — there is no env-var gate.
"""

# The deepagents / langchain optional deps cannot install on Python 3.10
# (deepagents pins >=3.11), so pyright cannot resolve their imports there;
# runtime collection is guarded by importorskip below.
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

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

from temporalio import workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent
    from langchain_core.messages import AIMessage

    from temporalio.contrib.deepagents import DeepAgentsPlugin, tool_as_activity
    from temporalio.contrib.deepagents.testing import mock_model_provider

INVOKE_MODEL = "deepagents.invoke_model"
INVOKE_TOOL = "deepagents.invoke_tool"


@workflow.defn
class DeepAgentWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            system_prompt="You are a helpful assistant.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        return result["messages"][-1].content


@workflow.defn
class ToolLoopWorkflow:
    @workflow.run
    async def run(self, city: str) -> str:
        def get_weather(city: str) -> str:
            """Return the weather for a city."""
            return f"It is sunny in {city}."

        weather_tool = tool_as_activity(
            get_weather, start_to_close_timeout=timedelta(seconds=30)
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            tools=[weather_tool],
            system_prompt="Use the weather tool to answer.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": f"Weather in {city}?"}]}
        )
        return result["messages"][-1].content


@pytest.mark.asyncio
async def test_deep_agent_runs_via_plugin(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["The answer is 42."]),
    )
    async with Worker(
        env.client,
        task_queue="da-native",
        workflows=[DeepAgentWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            DeepAgentWorkflow.run,
            "What is the meaning of life?",
            id=f"da-native-{uuid.uuid4()}",
            task_queue="da-native",
        )
        out = await handle.result()

    assert "42" in out
    counts = await count_scheduled_activities(handle)
    # The model call was made durable as an activity, with no user wiring.
    assert counts[INVOKE_MODEL] >= 1, counts


@pytest.mark.asyncio
async def test_agent_tool_loop_routes_to_activities(env: WorkflowEnvironment) -> None:
    # Script the model: first turn asks for the tool, second turn answers. This
    # forces model -> tool -> model, proving each call routes through an activity.
    tool_call = AIMessage(
        content="",
        tool_calls=[{"name": "get_weather", "args": {"city": "Paris"}, "id": "call-1"}],
    )
    final = AIMessage(content="It is sunny in Paris.")
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider([tool_call, final]),
    )
    async with Worker(
        env.client,
        task_queue="da-tool-loop",
        workflows=[ToolLoopWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ToolLoopWorkflow.run,
            "Paris",
            id=f"da-tool-loop-{uuid.uuid4()}",
            task_queue="da-tool-loop",
        )
        out = await handle.result()

    assert "Paris" in out
    counts = await count_scheduled_activities(handle)
    assert counts[INVOKE_MODEL] == 2, counts
    assert counts[INVOKE_TOOL] == 1, counts
