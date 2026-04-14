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

Requires `langgraph==1.1.3` and `temporalio>=1.24.0`.

## Plugin Initialization

### Graph API

```python
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(graphs={"my-graph": graph})
```

### Functional API

```python
import datetime
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(
    entrypoints={"my_entrypoint": my_entrypoint},
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

## Activity Options

Options are passed through to [`workflow.execute_activity()`](https://python.temporal.io/temporalio.workflow.html#execute_activity), which supports parameters like `start_to_close_timeout`, `retry_policy`, `schedule_to_close_timeout`, `heartbeat_timeout`, and more.

### Graph API

Pass activity options as node `metadata` when calling `add_node`:

```python
import datetime
from temporalio.common import RetryPolicy

g = StateGraph(State)
g.add_node("my_node", my_node, metadata={
    "start_to_close_timeout": datetime.timedelta(seconds=30),
    "retry_policy": RetryPolicy(maximum_attempts=3),
})
```

### Functional API

Pass activity options to the `Plugin` constructor, keyed by task function name:

```python
import datetime
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin

plugin = LangGraphPlugin(
    entrypoints={"my_entrypoint": my_entrypoint},
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
graph.add_node("my_node", my_node, metadata={"execute_in": "workflow"})

# Functional API
plugin = LangGraphPlugin(
    tasks=[my_task],
    activity_options={"my_task": {"execute_in": "workflow"}},
)
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
