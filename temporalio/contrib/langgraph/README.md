# LangGraph Plugin for Temporal Python SDK

⚠️ **This package is currently at an experimental release stage.** ⚠️

This Temporal [Plugin](https://docs.temporal.io/develop/plugins-guide) allows you to run [LangGraph](https://www.langchain.com/langgraph) nodes and tasks as Temporal Activities, giving your AI workflows durable execution, automatic retries, and timeouts. It supports both the LangGraph Graph API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).

## Installation

```sh
uv add temporalio[langgraph]
```

or with pip:

```sh
pip install temporalio[langgraph]
```

## Module layout

Define your graphs, tasks, and entrypoints in a module **separate** from your `@workflow.defn` classes — the standard Temporal split. The plugin adds the graph/task modules to the workflow sandbox's passthrough list so its in-place rewrites are visible to the workflow. Workflow modules stay sandboxed.

## Graph API

```python
# graphs.py
from langgraph.graph import START, StateGraph

my_graph = StateGraph(State)
my_graph.add_node("my_node", my_node)
my_graph.add_edge(START, "my_node")

# workflow.py
from temporalio import workflow
from myapp.graphs import my_graph

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input):
        return await my_graph.compile().ainvoke(input)

# worker.py
from temporalio.contrib.langgraph import LangGraphPlugin
from myapp.graphs import my_graph

plugin = LangGraphPlugin(graphs=[my_graph])
```

## Functional API

```python
# flows.py
from langgraph.func import entrypoint, task

@task
async def my_task(x): ...

@entrypoint()
async def my_flow(inputs):
    return await my_task(inputs)

# workflow.py
from temporalio import workflow
from myapp.flows import my_flow

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input):
        return await my_flow.ainvoke(input)

# worker.py
import datetime
from temporalio.contrib.langgraph import LangGraphPlugin
from myapp.flows import my_task

plugin = LangGraphPlugin(
    tasks=[my_task],
    activity_options={
        "my_task": {
            "start_to_close_timeout": datetime.timedelta(seconds=30),
        },
    },
)
```

## Checkpointer

Use `InMemorySaver` as your checkpointer. Temporal handles durability, so third-party checkpointers (like PostgreSQL or Redis) are not needed.

```python
import langgraph.checkpoint.memory

from temporalio import workflow
from myapp.graphs import my_graph

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input):
        app = my_graph.compile(
            checkpointer=langgraph.checkpoint.memory.InMemorySaver(),
        )
        ...
```

## Activity Options

Options are passed through to [`workflow.execute_activity()`](https://python.temporal.io/temporalio.workflow.html#execute_activity), which supports parameters like `start_to_close_timeout`, `retry_policy`, `schedule_to_close_timeout`, `heartbeat_timeout`, and more.

### Graph API

Pass per-node options as node `metadata`, or plugin-wide defaults via `default_activity_options`:

```python
import datetime
from temporalio.common import RetryPolicy

g = StateGraph(State)
g.add_node("my_node", my_node, metadata={
    "start_to_close_timeout": datetime.timedelta(seconds=30),
    "retry_policy": RetryPolicy(maximum_attempts=3),
})

plugin = LangGraphPlugin(
    graphs=[g],
    default_activity_options={"start_to_close_timeout": datetime.timedelta(seconds=60)},
)
```

### Functional API

Pass activity options to the plugin, keyed by task function name:

```python
import datetime
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(
    tasks=[my_task],
    activity_options={
        "my_task": {
            "start_to_close_timeout": datetime.timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        },
    },
)
```

### Running in the Workflow

To skip the Activity wrapper and run a node or task directly in the Workflow, set `execute_in` to `"workflow"`:

```python
# Graph API
g.add_node("my_node", my_node, metadata={"execute_in": "workflow"})

# Functional API
plugin = LangGraphPlugin(
    tasks=[my_task],
    activity_options={"my_task": {"execute_in": "workflow"}},
)
```

## Continue-As-New

To carry cached task results across a continue-as-new boundary, pass the cache to your next run and restore it with `set_cache`:

```python
from temporalio import workflow
from temporalio.contrib.langgraph import cache, set_cache
from myapp.graphs import my_graph

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input, prev_cache=None):
        set_cache(prev_cache)
        result = await my_graph.compile().ainvoke(input)
        if should_continue(result):
            workflow.continue_as_new(next_input, cache())
        return result
```

## Running Tests

Install dependencies:

```sh
uv sync
```

Run the test suite:

```sh
uv run pytest
```

Tests start a local Temporal dev server automatically — no external server needed.
