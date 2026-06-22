"""Tests for dynamic node/task summaries (summary_fn / default_summary_fn)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Callable

import pytest
from langchain_core.runnables import (
    RunnableConfig,  # pyright: ignore[reportMissingTypeStubs]
)
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

import temporalio.api.sdk.v1
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.helpers import assert_eq_eventually

SummaryFn = Callable[[tuple[Any, ...], dict[str, Any]], "str | None"]


class State(TypedDict):
    value: str


async def passthrough(state: State) -> dict[str, str]:
    return {"value": state["value"]}


def summarize(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],  # pyright: ignore[reportUnusedParameter]
) -> str | None:
    return f"value={args[0]['value']}"


@workflow.defn
class SummaryWorkflow:
    def __init__(self) -> None:
        self.app = graph("summary-graph").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


def _activity_graph(
    summary_fn: SummaryFn | None,
) -> StateGraph[State, None, State, State]:
    metadata: dict[str, Any] = {"execute_in": "activity"}
    if summary_fn is not None:
        metadata["summary_fn"] = summary_fn
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node("node", passthrough, metadata=metadata)
    g.add_edge(START, "node")
    return g


async def _run_and_collect_summaries(
    client: Client, plugin: LangGraphPlugin, input: str
) -> list[bytes]:
    task_queue = f"summary-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SummaryWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            SummaryWorkflow.run,
            input,
            id=f"summary-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()
        return [
            e.user_metadata.summary.data
            async for e in handle.fetch_history_events()
            if e.HasField("activity_task_scheduled_event_attributes")
        ]


def _plugin(g: StateGraph[Any, Any, Any, Any], **kwargs: Any) -> LangGraphPlugin:
    return LangGraphPlugin(
        graphs={"summary-graph": g},
        default_activity_options={"start_to_close_timeout": timedelta(seconds=10)},
        **kwargs,
    )


async def test_activity_summary_fn_in_history(client: Client) -> None:
    plugin = _plugin(_activity_graph(summarize))
    summaries = await _run_and_collect_summaries(client, plugin, "hello")
    assert summaries == [b'"value=hello"']


@pytest.mark.parametrize(
    "summary_fn,expected",
    [
        (lambda args, kwargs: f"value={args[0]['value']}", b'"value=x"'),
        (lambda args, kwargs: None, b""),
        (lambda args, kwargs: "", b""),
    ],
)
async def test_summary_fn_variants(
    client: Client, summary_fn: SummaryFn, expected: bytes
) -> None:
    plugin = _plugin(_activity_graph(summary_fn))
    summaries = await _run_and_collect_summaries(client, plugin, "x")
    assert summaries == [expected]


async def test_default_summary_fn(client: Client) -> None:
    plugin = _plugin(
        _activity_graph(None),
        default_summary_fn=lambda args, kwargs: "defaulted",
    )
    summaries = await _run_and_collect_summaries(client, plugin, "x")
    assert summaries == [b'"defaulted"']


async def test_per_node_summary_fn_overrides_default(client: Client) -> None:
    plugin = _plugin(
        _activity_graph(lambda args, kwargs: "per-node"),
        default_summary_fn=lambda args, kwargs: "defaulted",
    )
    summaries = await _run_and_collect_summaries(client, plugin, "x")
    assert summaries == [b'"per-node"']


async def test_static_summary_suppresses_default(client: Client) -> None:
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node(
        "node", passthrough, metadata={"execute_in": "activity", "summary": "static"}
    )
    g.add_edge(START, "node")
    plugin = _plugin(g, default_summary_fn=lambda args, kwargs: "defaulted")
    summaries = await _run_and_collect_summaries(client, plugin, "x")
    assert summaries == [b'"static"']


def test_summary_and_summary_fn_raises() -> None:
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node(
        "node",
        passthrough,
        metadata={
            "execute_in": "activity",
            "summary": "static",
            "summary_fn": lambda args, kwargs: "dynamic",
        },
    )
    g.add_edge(START, "node")
    with pytest.raises(ValueError, match="not both"):
        LangGraphPlugin(graphs={f"summary-graph-{uuid.uuid4()}": g})


async def node_reads_meta(state: State, config: RunnableConfig) -> dict[str, str]:
    metadata = config.get("metadata") or {}
    return {"value": f"{state['value']}-has_fn={'summary_fn' in metadata}"}


async def test_summary_fn_not_in_node_metadata(client: Client) -> None:
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node(
        "node",
        node_reads_meta,
        metadata={
            "execute_in": "activity",
            "summary_fn": lambda args, kwargs: "dynamic",
            "my_key": "my_value",
        },
    )
    g.add_edge(START, "node")
    task_queue = f"summary-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SummaryWorkflow],
        plugins=[_plugin(g)],
    ):
        result = await client.execute_workflow(
            SummaryWorkflow.run,
            "in",
            id=f"summary-{uuid.uuid4()}",
            task_queue=task_queue,
        )
    assert result == {"value": "in-has_fn=False"}


@workflow.defn
class WorkflowNodeSummaryWorkflow:
    def __init__(self) -> None:
        self.app = graph("wf-node-graph").compile()
        self._done = False

    @workflow.run
    async def run(self, input: str) -> Any:
        result = await self.app.ainvoke({"value": input})
        await workflow.wait_condition(lambda: self._done)
        return result

    @workflow.signal
    def finish(self) -> None:
        self._done = True

    @workflow.query
    def ran(self) -> bool:
        return workflow.get_current_details() != ""


async def test_workflow_node_sets_current_details(
    client: Client, env: WorkflowEnvironment
) -> None:
    if env.supports_time_skipping:
        pytest.skip("metadata query unreliable on the time-skipping test server")
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node(
        "node",
        passthrough,
        metadata={
            "execute_in": "workflow",
            "summary_fn": lambda args, kwargs: f"wf:{args[0]['value']}",
        },
    )
    g.add_edge(START, "node")
    task_queue = f"wf-node-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[WorkflowNodeSummaryWorkflow],
        plugins=[LangGraphPlugin(graphs={"wf-node-graph": g})],
    ):
        handle = await client.start_workflow(
            WorkflowNodeSummaryWorkflow.run,
            "ready",
            id=f"wf-node-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await assert_eq_eventually(
            True, lambda: handle.query(WorkflowNodeSummaryWorkflow.ran)
        )
        md: temporalio.api.sdk.v1.WorkflowMetadata = await handle.query(
            "__temporal_workflow_metadata",
            result_type=temporalio.api.sdk.v1.WorkflowMetadata,
        )
        assert md.current_details == "wf:ready"
        await handle.signal(WorkflowNodeSummaryWorkflow.finish)
        assert await handle.result() == {"value": "ready"}


async def test_replay_with_summary_fn(client: Client) -> None:
    plugin = _plugin(_activity_graph(summarize))
    task_queue = f"summary-replay-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SummaryWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            SummaryWorkflow.run,
            "hello",
            id=f"summary-replay-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()

    await Replayer(workflows=[SummaryWorkflow], plugins=[plugin]).replay_workflow(
        await handle.fetch_history()
    )
