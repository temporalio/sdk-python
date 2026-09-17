import sys
import warnings
from datetime import timedelta
from uuid import uuid4

import pytest
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Replayer, Worker
from tests.contrib.langgraph.test_interrupt import (
    InterruptWorkflow,
)
from tests.contrib.langgraph.test_interrupt import (
    State as InterruptState,
)
from tests.contrib.langgraph.test_interrupt import (
    node as interrupt_node,
)
from tests.contrib.langgraph.test_two_nodes import (
    State,
    TwoNodesWorkflow,
    node_a,
    node_b,
)


async def test_replay(client: Client):
    g = StateGraph(State)
    g.add_node("node_a", node_a, metadata={"execute_in": "activity"})
    g.add_node("node_b", node_b, metadata={"execute_in": "activity"})
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")

    task_queue = f"my-graph-{uuid4()}"
    plugin = LangGraphPlugin(
        graphs={"my-graph": g},
        default_activity_options={"start_to_close_timeout": timedelta(seconds=10)},
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TwoNodesWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            TwoNodesWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()

    with warnings.catch_warnings(record=True) as recorder:
        warnings.filterwarnings(
            "always", message=r"Module .* was imported after initial workflow load"
        )
        await Replayer(
            workflows=[TwoNodesWorkflow],
            plugins=[plugin],
        ).replay_workflow(await handle.fetch_history())

    # Sandbox imports during an activation count toward the deadlock timeout
    assert not [
        str(w.message)
        for w in recorder
        if "was imported after initial workflow load" in str(w.message)
    ]


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="langgraph.types.interrupt() requires Python >= 3.11 for async context propagation",
)
async def test_replay_interrupt(client: Client):
    g = StateGraph(InterruptState)
    g.add_node("node", interrupt_node, metadata={"execute_in": "activity"})
    g.add_edge(START, "node")

    task_queue = f"interrupt-replay-{uuid4()}"
    plugin = LangGraphPlugin(
        graphs={"my-graph": g},
        default_activity_options={"start_to_close_timeout": timedelta(seconds=10)},
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterruptWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            InterruptWorkflow.run,
            "",
            id=f"test-interrupt-replay-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.result()

    await Replayer(
        workflows=[InterruptWorkflow],
        plugins=[plugin],
    ).replay_workflow(await handle.fetch_history())
