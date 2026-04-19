import sys
from datetime import timedelta
from uuid import uuid4

import pytest

from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin
from temporalio.worker import Replayer, Worker
from tests.contrib.langgraph.test_interrupt import (
    InterruptWorkflow,
    interrupt_graph,
)
from tests.contrib.langgraph.test_two_nodes import (
    TwoNodesWorkflow,
    my_graph,
)

_DEFAULTS = {"start_to_close_timeout": timedelta(seconds=10)}


async def test_replay(client: Client):
    task_queue = f"my-graph-{uuid4()}"
    plugin = LangGraphPlugin(graphs=[my_graph], default_activity_options=_DEFAULTS)

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

    await Replayer(
        workflows=[TwoNodesWorkflow],
        plugins=[plugin],
    ).replay_workflow(await handle.fetch_history())


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="langgraph.types.interrupt() requires Python >= 3.11 for async context propagation",
)
async def test_replay_interrupt(client: Client):
    task_queue = f"interrupt-replay-{uuid4()}"
    plugin = LangGraphPlugin(
        graphs=[interrupt_graph], default_activity_options=_DEFAULTS
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
