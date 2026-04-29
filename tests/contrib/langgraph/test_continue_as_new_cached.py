"""Test Graph API continue-as-new with task result caching.

Verifies that node results are cached across continue-as-new boundaries,
so nodes don't re-execute when the graph is re-invoked with the same state.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, cache, graph
from temporalio.worker import Worker

# Track execution counts to verify caching
_execution_counts: dict[str, int] = {}


def _reset():
    _execution_counts.clear()


class State(TypedDict):
    value: int


async def multiply_by_3(state: State) -> dict[str, int]:
    _execution_counts["multiply"] = _execution_counts.get("multiply", 0) + 1
    return {"value": state["value"] * 3}


async def add_100(state: State) -> dict[str, int]:
    _execution_counts["add"] = _execution_counts.get("add", 0) + 1
    return {"value": state["value"] + 100}


async def double(state: State) -> dict[str, int]:
    _execution_counts["double"] = _execution_counts.get("double", 0) + 1
    return {"value": state["value"] * 2}


@dataclass
class GraphContinueAsNewInput:
    value: int
    cache: dict[str, Any] | None = None
    phase: int = 1  # 1, 2, 3 — continues-as-new after phases 1 and 2


@workflow.defn
class GraphContinueAsNewWorkflow:
    """Runs a 3-node graph, continuing-as-new after each phase.

    Phase 1: runs graph (all 3 nodes execute), continues-as-new with cache.
    Phase 2: runs graph again with same input (all 3 cached), continues-as-new.
    Phase 3: runs graph again with same input (all 3 cached), returns result.

    Without caching: each node executes 3 times.
    With caching: each node executes once (first run), cached for phases 2 & 3.
    """

    @workflow.run
    async def run(self, input_data: GraphContinueAsNewInput) -> dict[str, int]:
        app = graph("cached-graph", cache=input_data.cache).compile()
        result = await app.ainvoke({"value": input_data.value})

        if input_data.phase < 3:
            workflow.continue_as_new(
                GraphContinueAsNewInput(
                    value=input_data.value,
                    cache=cache(),
                    phase=input_data.phase + 1,
                )
            )

        return result


async def test_graph_continue_as_new_cached(client: Client):
    """Each node executes once despite 3 continue-as-new cycles.

    Graph: multiply_by_3 -> add_100 -> double
    Input 10: 10 * 3 = 30 -> 30 + 100 = 130 -> 130 * 2 = 260
    """
    _reset()

    metadata = {
        "execute_in": "activity",
        "start_to_close_timeout": timedelta(seconds=10),
    }
    g = StateGraph(State)
    g.add_node("multiply_by_3", multiply_by_3, metadata=metadata)
    g.add_node("add_100", add_100, metadata=metadata)
    g.add_node("double", double, metadata=metadata)
    g.add_edge(START, "multiply_by_3")
    g.add_edge("multiply_by_3", "add_100")
    g.add_edge("add_100", "double")

    task_queue = f"graph-cached-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[GraphContinueAsNewWorkflow],
        plugins=[LangGraphPlugin(graphs={"cached-graph": g})],
    ):
        result = await client.execute_workflow(
            GraphContinueAsNewWorkflow.run,
            GraphContinueAsNewInput(value=10),
            id=f"graph-cached-{uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )

    # 10 * 3 = 30 -> + 100 = 130 -> * 2 = 260
    assert result == {"value": 260}

    # Each node should execute exactly once — phases 2 and 3 use cached results.
    assert (
        _execution_counts.get("multiply", 0) == 1
    ), f"multiply executed {_execution_counts.get('multiply', 0)} times, expected 1"
    assert (
        _execution_counts.get("add", 0) == 1
    ), f"add executed {_execution_counts.get('add', 0)} times, expected 1"
    assert (
        _execution_counts.get("double", 0) == 1
    ), f"double executed {_execution_counts.get('double', 0)} times, expected 1"
