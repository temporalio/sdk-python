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

"""Integration tests for durable human-in-the-loop with ADK in Temporal workflows."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.events import RequestInput
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.adk.utils.context_utils import Aclosing
from google.adk.workflow import START, JoinNode, Workflow
from google.genai import types
from google.genai.types import Content, FunctionCall, Part

import temporalio.contrib.google_adk_agents.workflow
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.google_adk_agents import (
    GoogleAdkPlugin,
    HitlRequest,
    TemporalModel,
    hitl_confirmation_response,
    hitl_input_response,
    pending_hitl_requests,
)
from temporalio.worker import Replayer, Worker

TASK_QUEUE = "adk-hitl-task-queue"

# Worker-side record of real activity executions, keyed by workflow id.
_ACTIVITY_EXECUTIONS: dict[str, list[Any]] = {}


def _adk_routes_interrupt_ids_through_platform() -> bool:
    """Whether ADK mints default RequestInput ids via the platform uuid seam."""
    import google.adk.platform.uuid as platform_uuid

    platform_uuid.set_id_provider(lambda: "probe-id")
    try:
        return RequestInput().interrupt_id == "probe-id"  # type: ignore
    finally:
        platform_uuid.reset_id_provider()


@activity.defn
async def danger_activity(target: str) -> str:
    """Activity gated behind human confirmation."""
    _ACTIVITY_EXECUTIONS.setdefault(str(activity.info().workflow_id), []).append(target)
    return f"deleted-{target}"


class _HitlLoopMixin:
    """Shared pending/response bookkeeping for HITL workflows."""

    def __init__(self) -> None:
        self._pending: dict[str, HitlRequest] = {}
        self._responses: dict[str, Any] = {}

    async def _drive(self, runner: Runner, first_message: types.Content) -> str:
        session = await runner.session_service.create_session(
            app_name="test_app", user_id="test"
        )
        message = first_message
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
                    output = getattr(event, "output", None)
                    if output is not None:
                        result = str(output)
                    elif (
                        event.content
                        and event.content.parts
                        and event.content.parts[0].text
                    ):
                        result = event.content.parts[0].text
            if not self._pending:
                return result
            await workflow.wait_condition(
                lambda: any(i in self._responses for i in self._pending)
            )
            parts = []
            for interrupt_id in list(self._pending):
                if interrupt_id not in self._responses:
                    continue
                request = self._pending.pop(interrupt_id)
                response = self._responses.pop(interrupt_id)
                if request.kind == "tool_confirmation":
                    parts.append(
                        hitl_confirmation_response(
                            interrupt_id, confirmed=bool(response)
                        )
                    )
                else:
                    parts.append(hitl_input_response(interrupt_id, response))
            message = types.Content(role="user", parts=parts)


@workflow.defn
class HumanInputGraphWorkflow(_HitlLoopMixin):
    """A graph human-input node maps onto a durable Temporal wait."""

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    async def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        def approval_gate():
            yield RequestInput(  # type: ignore
                interrupt_id="approval", message="Approve the plan?"
            )

        def formatter(node_input: Any) -> str:
            return f"approved:{node_input}"

        graph = Workflow(name="hitl_graph", edges=[(START, approval_gate, formatter)])
        runner = Runner(
            app_name="test_app", node=graph, session_service=InMemorySessionService()
        )
        return await self._drive(
            runner, types.Content(role="user", parts=[types.Part(text=prompt)])
        )


class ConfirmationModel(BaseLlm):
    """Scripted model: call danger_activity once, then acknowledge its response."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        saw_tool_response = any(
            part.function_response is not None
            and part.function_response.name == "danger_activity"
            for content in llm_request.contents
            for part in content.parts or []
        )
        saw_confirmation_request = any(
            part.function_call is not None
            and part.function_call.name == "adk_request_confirmation"
            for content in llm_request.contents
            for part in content.parts or []
        )
        if saw_tool_response:
            yield LlmResponse(
                content=Content(role="model", parts=[Part(text="all-done")])
            )
        elif saw_confirmation_request:
            # The confirmation is pending; end the turn while waiting.
            yield LlmResponse(
                content=Content(role="model", parts=[Part(text="waiting-for-approval")])
            )
        else:
            yield LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="danger_activity", args={"target": "prod"}
                            )
                        )
                    ],
                )
            )

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["confirmation_model"]


@workflow.defn
class ConfirmationAgentWorkflow(_HitlLoopMixin):
    """Tool confirmation gates an activity_as_tool: the activity only runs on approval."""

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    async def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, model_name: str) -> str:
        danger_tool = temporalio.contrib.google_adk_agents.workflow.activity_as_tool(
            danger_activity, start_to_close_timeout=timedelta(seconds=30)
        )
        agent = LlmAgent(
            name="ops_agent",
            model=TemporalModel(model_name),
            instruction="You are an ops agent",
            tools=[FunctionTool(func=danger_tool, require_confirmation=True)],
        )
        runner = Runner(
            app_name="test_app", agent=agent, session_service=InMemorySessionService()
        )
        return await self._drive(
            runner,
            types.Content(role="user", parts=[types.Part(text="delete prod")]),
        )


@workflow.defn
class MultiPendingGraphWorkflow(_HitlLoopMixin):
    """Two parallel human-input branches; partial responses keep the rest pending."""

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    async def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        def gate_a():
            yield RequestInput(interrupt_id="a", message="Approve A?")  # type: ignore

        def gate_b():
            yield RequestInput(interrupt_id="b", message="Approve B?")  # type: ignore

        def combine(node_input: dict[str, Any]) -> str:
            return f"{node_input['gate_a']}&{node_input['gate_b']}"

        join = JoinNode(name="join")
        graph = Workflow(
            name="multi_hitl",
            edges=[
                (START, gate_a, join),
                (START, gate_b, join),
                (join, combine),
            ],
        )
        runner = Runner(
            app_name="test_app", node=graph, session_service=InMemorySessionService()
        )
        return await self._drive(
            runner, types.Content(role="user", parts=[types.Part(text=prompt)])
        )


@workflow.defn
class DefaultInterruptIdWorkflow(_HitlLoopMixin):
    """A RequestInput with no explicit id relies on the platform uuid seam."""

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    async def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        def unnamed_gate():
            yield RequestInput(message="Approve?")  # type: ignore

        def formatter(node_input: Any) -> str:
            return f"got:{node_input}"

        graph = Workflow(name="default_id", edges=[(START, unnamed_gate, formatter)])
        runner = Runner(
            app_name="test_app", node=graph, session_service=InMemorySessionService()
        )
        return await self._drive(
            runner, types.Content(role="user", parts=[types.Part(text=prompt)])
        )


def _adk_client(client: Client) -> Client:
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    return Client(**new_config)


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[danger_activity],
        workflows=[
            HumanInputGraphWorkflow,
            ConfirmationAgentWorkflow,
            MultiPendingGraphWorkflow,
            DefaultInterruptIdWorkflow,
        ],
        max_cached_workflows=0,
    )


async def _wait_for_pending(
    handle: WorkflowHandle,
    query: Any,
    count: int = 1,
    expected_ids: set[str] | None = None,
) -> list[HitlRequest]:
    async def _poll() -> list[HitlRequest]:
        while True:
            pending = await handle.query(query)
            if expected_ids is not None:
                if {p.interrupt_id for p in pending} == expected_ids:
                    return pending
            elif len(pending) >= count:
                return pending
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(_poll(), timeout=20)


@pytest.mark.asyncio
async def test_human_input_node_update_resume(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        handle = await client.start_workflow(
            HumanInputGraphWorkflow.run,
            "make a plan",
            id=f"hitl-input-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )
        pending = await _wait_for_pending(
            handle, HumanInputGraphWorkflow.pending_requests
        )
        assert pending[0].kind == "input"
        assert pending[0].interrupt_id == "approval"
        assert pending[0].message == "Approve the plan?"
        assert pending[0].invocation_id

        await handle.execute_update(
            HumanInputGraphWorkflow.respond, args=["approval", "ship-it"]
        )
        result = await handle.result()
    assert result == "approved:ship-it"


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.asyncio
async def test_tool_confirmation_activity_as_tool(client: Client, confirmed: bool):
    client = _adk_client(client)
    workflow_id = f"hitl-confirm-{confirmed}-{uuid.uuid4()}"
    # max_cached_workflows=0 forces a full history replay on every workflow
    # task, proving the confirmation resume is replay-safe: the recorded human
    # response references the confirmation function-call id, which must
    # regenerate identically on replay (it derives from workflow.uuid4() via
    # the platform uuid seam the plugin installs).
    async with _worker(client):
        LLMRegistry.register(ConfirmationModel)
        handle = await client.start_workflow(
            ConfirmationAgentWorkflow.run,
            "confirmation_model",
            id=workflow_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )
        pending = await _wait_for_pending(
            handle, ConfirmationAgentWorkflow.pending_requests
        )
        assert pending[0].kind == "tool_confirmation"
        assert pending[0].original_function_call is not None
        assert pending[0].original_function_call["name"] == "danger_activity"
        assert pending[0].original_function_call["args"] == {"target": "prod"}

        await handle.execute_update(
            ConfirmationAgentWorkflow.respond,
            args=[pending[0].interrupt_id, confirmed],
        )
        result = await handle.result()

    assert result == "all-done"
    executions = _ACTIVITY_EXECUTIONS.get(workflow_id, [])
    if confirmed:
        # The gated activity ran exactly once, only after approval.
        assert executions == ["prod"]
    else:
        # Rejected: the activity was never scheduled.
        assert executions == []


@pytest.mark.asyncio
async def test_hitl_multiple_pending_partial_response(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        handle = await client.start_workflow(
            MultiPendingGraphWorkflow.run,
            "go",
            id=f"hitl-multi-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )
        await _wait_for_pending(
            handle, MultiPendingGraphWorkflow.pending_requests, expected_ids={"a", "b"}
        )

        # Answer only one; the other must stay pending.
        await handle.execute_update(
            MultiPendingGraphWorkflow.respond, args=["a", "yes-a"]
        )
        await _wait_for_pending(
            handle, MultiPendingGraphWorkflow.pending_requests, expected_ids={"b"}
        )

        await handle.execute_update(
            MultiPendingGraphWorkflow.respond, args=["b", "yes-b"]
        )
        result = await handle.result()
    assert result == "yes-a&yes-b"


@pytest.mark.asyncio
async def test_default_interrupt_id_replay_safe(client: Client):
    if not _adk_routes_interrupt_ids_through_platform():
        pytest.skip(
            "requires google-adk with RequestInput ids routed through the"
            " platform uuid seam (upstream PR pending)"
        )
    client = _adk_client(client)
    async with _worker(client):
        handle = await client.start_workflow(
            DefaultInterruptIdWorkflow.run,
            "go",
            id=f"hitl-default-id-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )
        pending = await _wait_for_pending(
            handle, DefaultInterruptIdWorkflow.pending_requests
        )
        # The generated id must be a workflow-deterministic uuid.
        interrupt_id = pending[0].interrupt_id
        uuid.UUID(interrupt_id)

        await handle.execute_update(
            DefaultInterruptIdWorkflow.respond, args=[interrupt_id, "fine"]
        )
        result = await handle.result()
        assert result == "got:fine"

        # Replaying the full history must regenerate the same interrupt id.
        history = await handle.fetch_history()
    await Replayer(
        workflows=[DefaultInterruptIdWorkflow], plugins=[GoogleAdkPlugin()]
    ).replay_workflow(history)
