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

"""Integration tests for ADK v2 dynamic workflows running in Temporal workflows."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.utils.context_utils import Aclosing
from google.adk.workflow import START, Workflow, node
from google.genai import types
from google.genai.types import Content, FunctionCall, Part
from pydantic import BaseModel

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import (
    GoogleAdkPlugin,
    HitlRequest,
    TemporalModel,
    hitl_input_response,
    pending_hitl_requests,
)
from temporalio.contrib.google_adk_agents.workflow import activity_node
from temporalio.worker import Worker

TASK_QUEUE = "adk-dynamic-task-queue"

# Worker-side record of real activity executions, keyed by workflow id.
# Replayed workflow tasks do not re-execute activities, so this counts
# actual executions only.
_ACTIVITY_EXECUTIONS: dict[str, list[Any]] = {}


@activity.defn
async def enrich_number(n: int) -> str:
    """Activity that enriches a number."""
    return f"enriched-{n}"


@activity.defn
async def counted_fetch(tag: str) -> str:
    """Activity that records each real execution."""
    _ACTIVITY_EXECUTIONS.setdefault(str(activity.info().workflow_id), []).append(tag)
    return f"fetched-{tag}"


async def drive_graph(graph: Workflow, prompt: str) -> Any:
    """Runs an ADK graph to completion in-workflow, returning the last output."""
    runner = Runner(
        app_name="test_app", node=graph, session_service=InMemorySessionService()
    )
    session = await runner.session_service.create_session(
        app_name="test_app", user_id="test"
    )
    last_output: Any = None
    async with Aclosing(
        runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        )
    ) as agen:
        async for event in agen:
            if getattr(event, "output", None) is not None:
                last_output = event.output
    return last_output


@workflow.defn
class DynamicLoopWorkflow:
    """A dynamic node drives activity-backed children in a plain Python loop."""

    @workflow.run
    async def run(self, count: int) -> list[str]:
        child = activity_node(
            enrich_number, start_to_close_timeout=timedelta(seconds=30)
        )

        @node(rerun_on_resume=True)
        async def driver(ctx: Context) -> list[str]:
            results = []
            for i in range(count):
                results.append(await ctx.run_node(child, node_input=i))
            return results

        graph = Workflow(name="dynamic_loop", edges=[(START, driver)])
        return await drive_graph(graph, "go")


@workflow.defn
class DynamicGatherWorkflow:
    """A dynamic node fans out children concurrently with asyncio.gather."""

    @workflow.run
    async def run(self, count: int) -> list[str]:
        child = activity_node(
            enrich_number, start_to_close_timeout=timedelta(seconds=30)
        )

        @node(rerun_on_resume=True)
        async def driver(ctx: Context) -> list[str]:
            return list(
                await asyncio.gather(
                    *(ctx.run_node(child, node_input=i) for i in range(count))
                )
            )

        graph = Workflow(name="dynamic_gather", edges=[(START, driver)])
        return await drive_graph(graph, "go")


class EnrichInput(BaseModel):
    value: int


def _make_enrich_flow() -> Workflow:
    """An inner graph workflow usable as an agent tool (Workflow-as-Tool)."""

    def pick(node_input: EnrichInput) -> int:
        return node_input.value

    child = activity_node(enrich_number, start_to_close_timeout=timedelta(seconds=30))
    return Workflow(
        name="enrich_flow",
        description="Enriches a number and returns the enriched text.",
        input_schema=EnrichInput,
        edges=[(START, pick, child)],
    )


class WorkflowToolModel(BaseLlm):
    """Scripted model: call the enrich_flow tool once, then answer with its result."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        tool_response: types.FunctionResponse | None = None
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.function_response is not None:
                    tool_response = part.function_response
        if tool_response is None:
            yield LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="enrich_flow", args={"value": 7}
                            )
                        )
                    ],
                )
            )
        else:
            yield LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text=f"tool-said:{tool_response.response}")],
                )
            )

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["workflow_tool_model"]


@workflow.defn
class WorkflowAsToolWorkflow:
    """An agent invokes a whole graph workflow as a tool."""

    @workflow.run
    async def run(self, model_name: str) -> str:
        agent = LlmAgent(
            name="root",
            model=TemporalModel(model_name),
            instruction="Use the enrich_flow tool.",
            tools=[_make_enrich_flow()],  # type: ignore
        )
        runner = Runner(
            app_name="test_app",
            agent=agent,
            session_service=InMemorySessionService(),
        )
        session = await runner.session_service.create_session(
            app_name="test_app", user_id="test"
        )
        final_text = ""
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="enrich 7")]
                ),
            )
        ) as agen:
            async for event in agen:
                if event.content and event.content.parts:
                    text = event.content.parts[0].text
                    if text:
                        final_text = text
        return final_text


@workflow.defn
class DynamicResumeWorkflow:
    """HITL resume re-runs the dynamic driver but skips completed children."""

    def __init__(self) -> None:
        self._pending: dict[str, HitlRequest] = {}
        self._responses: dict[str, Any] = {}

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    async def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        child = activity_node(
            counted_fetch, start_to_close_timeout=timedelta(seconds=30)
        )

        def approval_gate():
            yield RequestInput(interrupt_id="approval", message="Approve?")  # type: ignore

        @node(rerun_on_resume=True)
        async def driver(ctx: Context) -> str:
            fetched = await ctx.run_node(child, node_input="step1")
            approval = await ctx.run_node(approval_gate)
            return f"{fetched}|{approval}"

        graph = Workflow(name="dynamic_resume", edges=[(START, driver)])
        runner = Runner(
            app_name="test_app", node=graph, session_service=InMemorySessionService()
        )
        session = await runner.session_service.create_session(
            app_name="test_app", user_id="test"
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        result = ""
        while True:
            async with Aclosing(
                runner.run_async(
                    user_id="test", session_id=session.id, new_message=message
                )
            ) as agen:
                async for event in agen:
                    for request in pending_hitl_requests(event):
                        self._pending[request.interrupt_id] = request
                    if getattr(event, "output", None) is not None:
                        result = str(event.output)
            if not self._pending:
                return result
            await workflow.wait_condition(
                lambda: any(i in self._responses for i in self._pending)
            )
            parts = [
                hitl_input_response(i, self._responses.pop(i))
                for i in list(self._pending)
                if i in self._responses
            ]
            for part in parts:
                assert part.function_response and part.function_response.id
                self._pending.pop(part.function_response.id)
            message = types.Content(role="user", parts=parts)


def _adk_client(client: Client) -> Client:
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    return Client(**new_config)


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[enrich_number, counted_fetch],
        workflows=[
            DynamicLoopWorkflow,
            DynamicGatherWorkflow,
            WorkflowAsToolWorkflow,
            DynamicResumeWorkflow,
        ],
        max_cached_workflows=0,
    )


@pytest.mark.asyncio
async def test_dynamic_loop(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            DynamicLoopWorkflow.run,
            3,
            id=f"dynamic-loop-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == ["enriched-0", "enriched-1", "enriched-2"]


@pytest.mark.asyncio
async def test_dynamic_gather(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            DynamicGatherWorkflow.run,
            4,
            id=f"dynamic-gather-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == ["enriched-0", "enriched-1", "enriched-2", "enriched-3"]


@pytest.mark.asyncio
async def test_workflow_as_tool(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        LLMRegistry.register(WorkflowToolModel)
        result = await client.execute_workflow(
            WorkflowAsToolWorkflow.run,
            "workflow_tool_model",
            id=f"workflow-as-tool-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert "enriched-7" in result


@pytest.mark.asyncio
async def test_dynamic_resume_skips_completed_children(client: Client):
    client = _adk_client(client)
    workflow_id = f"dynamic-resume-{uuid.uuid4()}"
    async with _worker(client):
        handle = await client.start_workflow(
            DynamicResumeWorkflow.run,
            "go",
            id=workflow_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )

        async def _pending() -> list[HitlRequest]:
            while True:
                pending = await handle.query(DynamicResumeWorkflow.pending_requests)
                if pending:
                    return pending
                await asyncio.sleep(0.1)

        pending = await asyncio.wait_for(_pending(), timeout=20)
        assert pending[0].kind == "input"
        assert pending[0].interrupt_id == "approval"
        assert pending[0].message == "Approve?"

        await handle.execute_update(
            DynamicResumeWorkflow.respond, args=["approval", "yes"]
        )
        result = await handle.result()

    assert result == "fetched-step1|yes"
    # The dynamic driver body re-ran on resume, but the completed activity
    # child was served from the session cache: exactly one real execution.
    assert _ACTIVITY_EXECUTIONS.get(workflow_id) == ["step1"]
