# LangGraph Plugin for Temporal Python SDK

⚠️ **This package is currently at an experimental release stage.** ⚠️

This Temporal [Plugin](https://docs.temporal.io/develop/plugins-guide) allows you to run [LangGraph](https://www.langchain.com/langgraph) nodes and tasks as Temporal Activities, giving your AI workflows durable execution, automatic retries, and timeouts. It supports both the LangGraph Graph API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).

## Installation

```sh
uv add temporalio[langgraph]
```

## Plugin Initialization

### Graph API

```python
from langgraph.graph import StateGraph
from temporalio.contrib.langgraph import LangGraphPlugin

g = StateGraph(State)
g.add_node("my_node", my_node, metadata={"execute_in": "activity"})

plugin = LangGraphPlugin(graphs={"my-graph": g})
```

### Functional API

```python
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(
    entrypoints={"my_entrypoint": my_entrypoint},
    tasks=[my_task],
    activity_options={"my_task": {"execute_in": "activity"}},
)
```

## Checkpointer

If your LangGraph code requires a checkpointer (for example, if you're using interrupts), use `InMemorySaver`.
Temporal handles durability, so third-party checkpointers (like PostgreSQL or Redis) are not needed.

```python
import langgraph.checkpoint.memory
import typing

from temporalio.contrib.langgraph import graph
from temporalio import workflow

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: str) -> typing.Any:
        g = graph("my-graph").compile(
            checkpointer=langgraph.checkpoint.memory.InMemorySaver(),
        )

        ...
```

## Execution Location

Every node (Graph API) and task (Functional API) must be labeled with `execute_in`, set to either `"activity"` or `"workflow"`. This is required per node/task; it cannot be set in `default_activity_options`.

```python
# Graph API
graph.add_node("my_node", my_node, metadata={"execute_in": "activity"})
graph.add_node("tool_node", tool_node, metadata={"execute_in": "workflow"})

# Functional API
plugin = LangGraphPlugin(
    tasks=[my_task, tool_task],
    activity_options={
        "my_task": {"execute_in": "activity"},
        "tool_task": {"execute_in": "workflow"},
    },
)
```

## Activity Options

Options are passed through to [`workflow.execute_activity()`](https://python.temporal.io/temporalio.workflow.html#execute_activity), which supports parameters like `start_to_close_timeout`, `retry_policy`, `schedule_to_close_timeout`, `heartbeat_timeout`, and more.

### Graph API

Pass Activity options as node `metadata` when calling `add_node`:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy

g = StateGraph(State)
g.add_node("my_node", my_node, metadata={
    "execute_in": "activity",
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(maximum_attempts=3),
})
```

### Functional API

Pass Activity options to the `LangGraphPlugin` constructor, keyed by task function name:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(
    entrypoints={"my_entrypoint": my_entrypoint},
    tasks=[my_task],
    activity_options={
        "my_task": {
            "execute_in": "activity",
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        },
    },
)
```

### Runtime Context

LangGraph's run-scoped context (`context_schema`) is reconstructed on the Activity side, so nodes and tasks can read from and write to `runtime.context`:

```python
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from temporalio.contrib.langgraph import graph

class Context(TypedDict):
    user_id: str

async def my_node(state: State, runtime: Runtime[Context]) -> dict:
    return {"user": runtime.context["user_id"]}

# In the Workflow:
g = graph("my-graph").compile()
await g.ainvoke({...}, context=Context(user_id="alice"))
```

Your `context` object must be serializable by the configured Temporal payload converter, since it crosses the Activity boundary.

## Streaming

When `streaming_topic` is set on `LangGraphPlugin`, calls to `langgraph.config.get_stream_writer()` inside a node publish to the named topic on the workflow's [`WorkflowStream`](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/workflow_streams). Activity-side nodes publish via `WorkflowStreamClient` (a signal carrying batched items, controlled by `streaming_batch_interval`); workflow-side nodes publish synchronously to the in-workflow stream (no signal). External subscribers consume the stream with `WorkflowStreamClient.create(...).topic(...).subscribe(...)`.

The workflow **must** construct `WorkflowStream()` in its `@workflow.init` (i.e. `__init__`)

```python
from datetime import timedelta
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import START, StateGraph
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def token_node(state: State) -> dict[str, str]:
    writer = get_stream_writer()
    for token in ["hello", " ", "world"]:
        writer({"token": token})
    writer({"done": True})
    return {"value": "hello world"}


@workflow.defn
class StreamingWorkflow:
    def __init__(self) -> None:
        # Required when streaming_topic is set on the plugin.
        _ = WorkflowStream()
        self.app = graph("streaming").compile()

    @workflow.run
    async def run(self) -> str:
        result = await self.app.ainvoke({"value": ""})
        return result["value"]


async def main(client: Client) -> None:
    g = StateGraph(State)
    g.add_node("token_node", token_node, metadata={"execute_in": "activity"})
    g.add_edge(START, "token_node")

    async with Worker(
        client,
        task_queue="streaming-tq",
        workflows=[StreamingWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"streaming": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
                streaming_topic="tokens",
            )
        ],
    ):
        handle = await client.start_workflow(
            StreamingWorkflow.run, id="streaming-wf", task_queue="streaming-tq"
        )

        ws_client = WorkflowStreamClient.create(client, handle.id)
        async for item in ws_client.topic("tokens", type=dict).subscribe(from_offset=0):
            print(item.data)
            if item.data.get("done"):
                break

        print(await handle.result())
```

### What's covered, and what isn't

`streaming_topic` wires up exactly **one** LangGraph stream mode: `stream_mode="custom"`, i.e. values written through `get_stream_writer()`. The other modes — `"messages"`, `"values"`, `"updates"`, `"debug"` — are **not** captured by `streaming_topic`. They aren't produced by node-side writers; LangGraph's orchestrator emits them as it walks the graph. The documented pattern is to **bridge `astream()` in the workflow** and republish each yielded chunk to a `WorkflowStream` topic yourself:

```python
@workflow.defn
class AstreamBridge:
    def __init__(self) -> None:
        self.stream = WorkflowStream()
        self.app = graph("g").compile()

    @workflow.run
    async def run(self) -> None:
        topic = self.stream.topic("astream")
        async for chunk in self.app.astream({...}, stream_mode="messages"):
            topic.publish(chunk)
        topic.publish({"done": True})
```

### Retry semantics

Streaming has **at-least-once** delivery per activity attempt. When an activity-wrapped node retries (transient failure, worker crash, etc.), the user function re-runs from scratch and re-publishes its writes — earlier publishes from the failed attempt are not rolled back. Subscribers should be ready to see duplicates and recover idempotently (e.g. dedupe on a sequence id you include in each chunk, or treat the stream as advisory and rely on the workflow's final result for state).

## Tracing

We recommend the [Temporal LangSmith Plugin](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/langsmith) to trace your LangGraph Workflows and Activities.

## Stores are not supported

LangGraph's `Store` (e.g. `InMemoryStore` passed via `graph.compile(store=...)` or `@entrypoint(store=...)`) isn't accessible inside Activity-wrapped nodes: the Store holds live state that can't cross the Activity boundary, and Activities may run on a different worker than the Workflow. If you pass a store, the plugin logs a warning on first use and `runtime.store` is `None` inside nodes.

Use Workflow state for per-run memory, or an external database (Postgres/Redis/etc.) configured on each worker if you need shared memory across runs.

## Running Tests

Install dependencies:

```sh
uv sync --all-extras
```

Run the test suite:

```sh
uv run pytest tests/contrib/langgraph
```

Tests start a local Temporal dev server automatically — no external server needed.
