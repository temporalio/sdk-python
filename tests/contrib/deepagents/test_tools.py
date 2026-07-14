"""The tool seam: existing activities and wrapped tools route to activities.

These need LangChain (a tool is a ``BaseTool``) and guard on its import. Each
runs a real workflow that invokes the wrapped tool and asserts it scheduled the
expected activity.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

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
        message = await tool.ainvoke({"city": city})
        return message.content


@pytest.mark.asyncio
async def test_activity_as_tool(env) -> None:
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
async def test_tool_as_activity(env) -> None:
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


def test_builtin_tool_in_workflow(recwarn) -> None:
    # Built-in tool names never warn (they are pure, in-workflow); an unwrapped
    # user tool does warn so the Workflow-vs-Activity choice is conscious.
    warn_unwrapped_tools([SimpleNamespace(name="write_todos")])
    assert len(recwarn) == 0

    warn_unwrapped_tools([SimpleNamespace(name="scrape_website")])
    assert any("scrape_website" in str(w.message) for w in recwarn)
