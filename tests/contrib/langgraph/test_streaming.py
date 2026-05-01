from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.config import (
    get_stream_writer,  # pyright: ignore[reportMissingTypeStubs]
)
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import STREAM_TOPIC, LangGraphPlugin, graph
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def token_node(state: State) -> dict[str, str]:
    tokens = ["a", "b", "c"]
    writer = get_stream_writer()
    for token in tokens:
        writer({"token": token})
    writer({"done": True})
    return {"value": state["value"] + "".join(tokens)}


@workflow.defn
class StreamingWorkflowStreamsWorkflow:
    def __init__(self) -> None:
        _ = WorkflowStream()
        self.app = graph("streaming-ws").compile()

    @workflow.run
    async def run(self, input: str) -> str:
        result = await self.app.ainvoke({"value": input})
        return result["value"]


async def test_streaming_via_workflow_streams(client: Client):
    g = StateGraph(State)
    g.add_node("token_node", token_node, metadata={"execute_in": "activity"})
    g.add_edge(START, "token_node")

    task_queue = f"streaming-ws-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[StreamingWorkflowStreamsWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"streaming-ws": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        handle = await client.start_workflow(
            StreamingWorkflowStreamsWorkflow.run,
            "",
            id=f"test-streaming-ws-{uuid4()}",
            task_queue=task_queue,
        )

        ws_client = WorkflowStreamClient.create(client, handle.id)
        chunks: list[dict[str, Any]] = []
        async for item in ws_client.topic(STREAM_TOPIC, type=dict).subscribe(
            from_offset=0,
            poll_cooldown=timedelta(0),
        ):
            chunks.append(item.data)
            if chunks[-1].get("done"):
                break

        result = await handle.result()

    assert result == "abc"
    assert chunks == [
        {"token": "a"},
        {"token": "b"},
        {"token": "c"},
        {"done": True},
    ]


# ---------------------------------------------------------------------------
# Workflow-side publish: iterate astream() in the workflow and forward each
# chunk via self.stream.topic("astream").publish(...) so external subscribers
# see node-level progress alongside any activity-emitted tokens.
# ---------------------------------------------------------------------------


@workflow.defn
class AstreamPublishWorkflow:
    def __init__(self) -> None:
        self.stream = WorkflowStream()
        self.app = graph("astream-publish").compile()

    @workflow.run
    async def run(self, input: str) -> str:
        topic = self.stream.topic("astream")
        async for chunk in self.app.astream({"value": input}):
            topic.publish(chunk)
        topic.publish({"done": True})
        return "done"


async def node_a(state: State) -> dict[str, str]:
    return {"value": state["value"] + "a"}


async def node_b(state: State) -> dict[str, str]:
    return {"value": state["value"] + "b"}


async def test_workflow_publishes_astream_chunks(client: Client):
    g = StateGraph(State)
    g.add_node("node_a", node_a, metadata={"execute_in": "activity"})
    g.add_node("node_b", node_b, metadata={"execute_in": "activity"})
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")

    task_queue = f"astream-publish-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[AstreamPublishWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"astream-publish": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        handle = await client.start_workflow(
            AstreamPublishWorkflow.run,
            "",
            id=f"test-astream-publish-{uuid4()}",
            task_queue=task_queue,
        )

        ws_client = WorkflowStreamClient.create(client, handle.id)
        chunks: list[dict[str, Any]] = []
        async for item in ws_client.topic("astream", type=dict).subscribe(
            from_offset=0,
            poll_cooldown=timedelta(0),
        ):
            chunks.append(item.data)
            if chunks[-1].get("done"):
                break

        await handle.result()

    assert chunks == [
        {"node_a": {"value": "a"}},
        {"node_b": {"value": "ab"}},
        {"done": True},
    ]
