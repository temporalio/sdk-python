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

"""Integration tests for ADK v2 graph workflows running in Temporal workflows."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.utils.context_utils import Aclosing
from google.adk.workflow import (
    DEFAULT_ROUTE,
    START,
    FunctionNode,
    JoinNode,
    NodeTimeoutError,
    RetryConfig,
    Workflow,
)
from google.genai import types
from google.genai.types import Content, Part

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.google_adk_agents.workflow import activity_node
from temporalio.worker import Replayer, Worker

TASK_QUEUE = "adk-graph-task-queue"


def _adk_has_random_seam() -> bool:
    """Whether ADK exposes the platform random provider seam."""
    import importlib.util

    return importlib.util.find_spec("google.adk.platform._random") is not None


@activity.defn
async def fetch_data(query: str) -> str:
    """Activity that fetches data for a query."""
    return f"data-for-{query}"


@activity.defn
async def enrich_item(item: str) -> str:
    """Activity that enriches a single item."""
    return f"enriched-{item}"


@activity.defn
async def combine_parts(left: str, right: str) -> str:
    """Activity that combines two named parts."""
    return f"{left}+{right}"


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
class SequentialGraphWorkflow:
    """START -> activity node -> plain in-workflow node."""

    @workflow.run
    async def run(self, query: str) -> str:
        fetch = activity_node(fetch_data, start_to_close_timeout=timedelta(seconds=30))

        def summarize(node_input: str) -> str:
            return f"{node_input} summarized"

        graph = Workflow(name="pipeline", edges=[(START, fetch, summarize)])
        return await drive_graph(graph, query)


@workflow.defn
class RoutingGraphWorkflow:
    """Conditional routing through a dict edge with a DEFAULT_ROUTE fallback."""

    @workflow.run
    async def run(self, ticket: str) -> str:
        from google.adk.events import Event

        def route_ticket(node_input: Any) -> Event:
            text = str(node_input)
            return Event(route="bug" if "bug" in text else "other", output=text)  # type: ignore

        def handle_bug(node_input: str) -> str:  # pyright: ignore[reportUnusedParameter]
            return "routed-to-bug"

        def handle_other(node_input: str) -> str:  # pyright: ignore[reportUnusedParameter]
            return "routed-to-other"

        graph = Workflow(
            name="router",
            edges=[
                (START, route_ticket),
                (route_ticket, {"bug": handle_bug, DEFAULT_ROUTE: handle_other}),
            ],
        )
        return await drive_graph(graph, ticket)


@workflow.defn
class ParallelJoinGraphWorkflow:
    """Parallel fan-out of two activity-backed branches joined by a JoinNode."""

    @workflow.run
    async def run(self, prompt: str) -> dict[str, Any]:
        def make_a(node_input: Any) -> str:  # pyright: ignore[reportUnusedParameter]
            return "alpha"

        def make_b(node_input: Any) -> str:  # pyright: ignore[reportUnusedParameter]
            return "beta"

        enrich_a = activity_node(
            enrich_item, name="enrich_a", start_to_close_timeout=timedelta(seconds=30)
        )
        enrich_b = activity_node(
            enrich_item, name="enrich_b", start_to_close_timeout=timedelta(seconds=30)
        )

        join = JoinNode(name="join")
        graph = Workflow(
            name="fanout",
            edges=[
                (START, make_a, enrich_a, join),
                (START, make_b, enrich_b, join),
            ],
        )
        result = await drive_graph(graph, prompt)
        assert isinstance(result, dict)
        return result


@workflow.defn
class MultiParamActivityNodeWorkflow:
    """A multi-parameter activity node bound from a dict node_input."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        def prepare(node_input: Any) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
            return {"left": "L", "right": "R"}

        combine = activity_node(
            combine_parts, start_to_close_timeout=timedelta(seconds=30)
        )
        graph = Workflow(name="multi", edges=[(START, prepare, combine)])
        return await drive_graph(graph, prompt)


@workflow.defn
class AgentNodeGraphWorkflow:
    """An LlmAgent node inside a graph, calling the model via an activity."""

    @workflow.run
    async def run(self, model_name: str) -> str:
        greeter = LlmAgent(
            name="greeter",
            model=TemporalModel(model_name),
            instruction="You are a greeter",
            mode="single_turn",
        )

        def finalize(node_input: Any) -> str:
            text = node_input
            if isinstance(node_input, types.Content) and node_input.parts:
                text = node_input.parts[0].text
            return f"final:{text}"

        graph = Workflow(name="agent_graph", edges=[(START, greeter, finalize)])
        return await drive_graph(graph, "greet the user")


@workflow.defn
class TimeoutGraphWorkflow:
    """A node timeout surfaces as NodeTimeoutError via a durable timer."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        async def slow(node_input: Any) -> str:  # pyright: ignore[reportUnusedParameter]
            await asyncio.sleep(5)
            return "never"

        slow_node = FunctionNode(func=slow, timeout=0.2)
        graph = Workflow(name="slowpoke", edges=[(START, slow_node)])
        try:
            await drive_graph(graph, prompt)
            return "no-timeout"
        except NodeTimeoutError:
            return "timed-out"


@workflow.defn
class RetryGraphWorkflow:
    """An ADK RetryConfig retries a failing in-workflow node deterministically."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        attempts: list[int] = []

        def flaky(node_input: Any) -> str:  # pyright: ignore[reportUnusedParameter]
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient failure")
            return f"ok-after-{len(attempts)}"

        flaky_node = FunctionNode(
            func=flaky,
            retry_config=RetryConfig(max_attempts=3, initial_delay=0.01, jitter=0.0),
        )
        graph = Workflow(name="retrier", edges=[(START, flaky_node)])
        return await drive_graph(graph, prompt)


@workflow.defn
class JitteredRetryGraphWorkflow:
    """A retried node with default-style jitter must replay deterministically.

    Retry jitter feeds asyncio.sleep, i.e. a durable timer; unless the delay is
    drawn from workflow.random() (via ADK's platform random seam), replays
    compute a different timer duration and diverge.
    """

    @workflow.run
    async def run(self, prompt: str) -> str:
        attempts: list[int] = []

        def flaky(node_input: Any) -> str:  # pyright: ignore[reportUnusedParameter]
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient failure")
            return f"ok-after-{len(attempts)}"

        flaky_node = FunctionNode(
            func=flaky,
            retry_config=RetryConfig(max_attempts=3, initial_delay=0.05, jitter=0.5),
        )
        graph = Workflow(name="jittery", edges=[(START, flaky_node)])
        return await drive_graph(graph, prompt)


class GraphAgentModel(BaseLlm):
    """Scripted model for the agent-node graph test."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=Content(role="model", parts=[Part(text="agent-says-hi")])
        )

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["graph_model"]


def _adk_client(client: Client) -> Client:
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    return Client(**new_config)


def _worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[fetch_data, enrich_item, combine_parts],
        workflows=[
            SequentialGraphWorkflow,
            RoutingGraphWorkflow,
            ParallelJoinGraphWorkflow,
            MultiParamActivityNodeWorkflow,
            AgentNodeGraphWorkflow,
            TimeoutGraphWorkflow,
            RetryGraphWorkflow,
            JitteredRetryGraphWorkflow,
        ],
        max_cached_workflows=0,
    )


@pytest.mark.asyncio
async def test_graph_sequential_with_activity_node(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            SequentialGraphWorkflow.run,
            "hello",
            id=f"graph-sequential-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "data-for-hello summarized"


@pytest.mark.parametrize(
    "ticket,expected",
    [("bug: crash", "routed-to-bug"), ("question", "routed-to-other")],
)
@pytest.mark.asyncio
async def test_graph_conditional_routing(client: Client, ticket: str, expected: str):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            RoutingGraphWorkflow.run,
            ticket,
            id=f"graph-routing-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == expected


@pytest.mark.asyncio
async def test_graph_parallel_fanout_join(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            ParallelJoinGraphWorkflow.run,
            "go",
            id=f"graph-join-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    # JoinNode aggregates branch outputs keyed by predecessor node name.
    assert result == {"enrich_a": "enriched-alpha", "enrich_b": "enriched-beta"}


@pytest.mark.asyncio
async def test_graph_multi_param_activity_node(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            MultiParamActivityNodeWorkflow.run,
            "go",
            id=f"graph-multiparam-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "L+R"


@pytest.mark.asyncio
async def test_graph_llm_agent_node(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        LLMRegistry.register(GraphAgentModel)
        result = await client.execute_workflow(
            AgentNodeGraphWorkflow.run,
            "graph_model",
            id=f"graph-agent-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "final:agent-says-hi"


@pytest.mark.asyncio
async def test_graph_node_timeout(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            TimeoutGraphWorkflow.run,
            "go",
            id=f"graph-timeout-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "timed-out"


@pytest.mark.asyncio
async def test_graph_node_retry(client: Client):
    client = _adk_client(client)
    async with _worker(client):
        result = await client.execute_workflow(
            RetryGraphWorkflow.run,
            "go",
            id=f"graph-retry-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "ok-after-2"


@pytest.mark.asyncio
async def test_graph_node_retry_jitter_replay_safe(client: Client):
    if not _adk_has_random_seam():
        pytest.skip(
            "requires google-adk with the platform random seam (upstream PR pending)"
        )
    client = _adk_client(client)
    async with _worker(client):
        handle = await client.start_workflow(
            JitteredRetryGraphWorkflow.run,
            "go",
            id=f"graph-retry-jitter-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
        )
        result = await handle.result()
        assert result == "ok-after-2"
        history = await handle.fetch_history()
    # The jittered retry delay is a durable timer; replay must recompute the
    # exact same duration from workflow.random().
    await Replayer(
        workflows=[JitteredRetryGraphWorkflow], plugins=[GoogleAdkPlugin()]
    ).replay_workflow(history)
