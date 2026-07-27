# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ToolContextSnapshot injection into activity-backed tools.

Covers https://github.com/temporalio/sdk-python/issues/1470: activities
wrapped with activity_tool can read the serializable subset of the ADK
ToolContext (session state and function-call id) without the live,
non-serializable ToolContext ever crossing the activity boundary, and
without the parameter leaking into the LLM-facing tool schema.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from google.adk import Agent
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from google.genai.types import Content, FunctionCall, Part

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.google_adk_agents.workflow import (
    ToolContextSnapshot,
    activity_tool,
)
from temporalio.worker import Worker

TASK_QUEUE = "adk-tool-context-task-queue"


@activity.defn
async def lookup_weather(
    city: str, tool_context: ToolContextSnapshot, units: str = "celsius"
) -> str:
    """Activity that reads tool configuration from session state.

    Mirrors the shape reported in issue #1470, with tool_context deliberately
    in the middle of the parameter list to prove positional slotting.
    """
    db_url = tool_context.state.get("db_url", "<missing>")
    has_function_call_id = "yes" if tool_context.function_call_id else "no"
    return f"{city}|{units}|{db_url}|fc={has_function_call_id}"


def weather_agent(model_name: str) -> Agent:
    return Agent(
        name="state_agent",
        model=TemporalModel(model_name),
        tools=[
            activity_tool(lookup_weather, start_to_close_timeout=timedelta(seconds=30))
        ],
    )


class StateToolModel(BaseLlm):
    """Scripted model: call lookup_weather once, then echo its response."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        tool_response = None
        for content in llm_request.contents:
            for part in content.parts or []:
                if (
                    part.function_response is not None
                    and part.function_response.name == "lookup_weather"
                ):
                    tool_response = part.function_response
        if tool_response is None:
            yield LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="lookup_weather", args={"city": "NYC"}
                            )
                        )
                    ],
                )
            )
        else:
            yield LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text=f"done:{tool_response.response}")],
                )
            )

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["state_tool_model"]


async def run_state_agent(model_name: str) -> str:
    """Runs the agent against a session seeded with state; returns final text."""
    runner = InMemoryRunner(agent=weather_agent(model_name), app_name="test_app")
    session = await runner.session_service.create_session(
        app_name="test_app",
        user_id="test",
        state={"db_url": "postgres://config"},
    )
    final_text = ""
    async with Aclosing(
        runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text="weather in NYC?")]
            ),
        )
    ) as agen:
        async for event in agen:
            if event.content and event.content.parts and event.content.parts[0].text:
                final_text = event.content.parts[0].text
    return final_text


@workflow.defn
class StateToolWorkflow:
    @workflow.run
    async def run(self, model_name: str) -> str:
        return await run_state_agent(model_name)


@pytest.mark.asyncio
async def test_activity_tool_receives_tool_context_snapshot(client: Client):
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[lookup_weather],
        workflows=[StateToolWorkflow],
        max_cached_workflows=0,
    ):
        LLMRegistry.register(StateToolModel)
        result = await client.execute_workflow(
            StateToolWorkflow.run,
            "state_tool_model",
            id=f"tool-context-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    # The activity saw the session state, the default parameter value, and a
    # populated function-call id — none of which came from the LLM.
    assert "NYC|celsius|postgres://config|fc=yes" in result


@pytest.mark.asyncio
async def test_activity_tool_snapshot_outside_workflow():
    """Local ADK runs (no Temporal) receive the same snapshot."""
    LLMRegistry.register(StateToolModel)
    result = await run_state_agent("state_tool_model")
    assert "NYC|celsius|postgres://config|fc=yes" in result


def _declared_properties(tool: FunctionTool) -> dict[str, Any]:
    """Property names in the LLM-facing declaration, across schema styles."""
    declaration = tool._get_declaration()
    assert declaration is not None
    if declaration.parameters_json_schema is not None:
        return declaration.parameters_json_schema.get("properties", {})
    assert declaration.parameters is not None
    return declaration.parameters.properties or {}


def test_tool_schema_excludes_tool_context():
    """The tool_context parameter never appears in the LLM-facing schema."""
    tool = FunctionTool(
        func=activity_tool(lookup_weather, start_to_close_timeout=timedelta(seconds=30))
    )
    properties = _declared_properties(tool)
    assert "city" in properties
    assert "units" in properties
    assert "tool_context" not in properties


def test_activity_tool_rejects_adk_tool_context_annotation():
    """Annotating with the live ADK ToolContext gives an actionable error."""

    @activity.defn
    async def bad_tool(query: str, tool_context: ToolContext) -> str:  # pyright: ignore[reportUnusedParameter]
        return query

    with pytest.raises(ValueError, match="ToolContextSnapshot"):
        activity_tool(bad_tool, start_to_close_timeout=timedelta(seconds=30))


def test_activity_tool_rejects_other_tool_context_annotation():
    """The reserved name with an unrelated annotation gives an actionable error."""

    @activity.defn
    async def confused_tool(query: str, tool_context: dict[str, Any]) -> str:  # pyright: ignore[reportUnusedParameter]
        return query

    with pytest.raises(ValueError, match="reserved by ADK"):
        activity_tool(confused_tool, start_to_close_timeout=timedelta(seconds=30))


def test_activity_tool_without_tool_context_unchanged():
    """Activities without a tool_context parameter keep their exact schema."""

    @activity.defn
    async def plain_tool(query: str) -> str:
        return query

    tool = FunctionTool(
        func=activity_tool(plain_tool, start_to_close_timeout=timedelta(seconds=30))
    )
    assert set(_declared_properties(tool)) == {"query"}
