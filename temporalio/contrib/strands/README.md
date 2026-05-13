# AWS Strands Agents

⚠️ **This package is currently at an experimental release stage.** ⚠️

This Temporal [Plugin](https://docs.temporal.io/develop/plugins-guide) allows you to run [Strands Agents](https://strandsagents.com/) inside Temporal Workflows, routing model invocations, tool calls, and MCP tool calls through Temporal Activities for durable execution, automatic retries, and timeouts.

## Installation

```sh
uv add temporalio[strands]
```

## Quickstart

`workflow.py` defines the workflow and runs the worker:

```python
import asyncio
from datetime import timedelta

from strands import Agent

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalModel
from temporalio.worker import Worker

MODEL = TemporalModel(start_to_close_timeout=timedelta(seconds=60))


@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = Agent(model=MODEL)

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        return str(result)


async def main() -> None:
    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue="strands",
        workflows=[MyWorkflow],
        plugins=[StrandsPlugin(model=MODEL)],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
```

`client.py` starts the workflow:

```python
import asyncio

from temporalio.client import Client

from workflow import MyWorkflow


async def main() -> None:
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(
        MyWorkflow.run,
        "Hello",
        id="strands-quickstart",
        task_queue="strands",
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

Note: Use `agent.invoke_async(message)` instead of `agent(message)`. The synchronous form spawns a worker thread, which the workflow sandbox blocks.

## Model

`TemporalModel` defaults to `BedrockModel()`. To use a different model (or a different `BedrockModel` configuration), pass a `model_factory` lambda. The plugin calls it once at worker startup.

```python
from strands.models.anthropic import AnthropicModel

MODEL = TemporalModel(
    model_factory=lambda: AnthropicModel(client_args={"api_key": "..."}),
    start_to_close_timeout=timedelta(seconds=60),
)
```

## Structured Output

Pass a Pydantic model to `Agent(structured_output_model=...)`. Strands routes the call through `stream()` as a synthetic tool, so it dispatches via the model activity like any other invocation. The result is available as `result.structured_output` and can be returned directly from the workflow — `StrandsPlugin` defaults to [`pydantic_data_converter`](../pydantic), so Pydantic types serialize across the activity and workflow boundary.

```python
from pydantic import BaseModel

class PersonInfo(BaseModel):
    name: str
    age: int

@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = Agent(model=MODEL, structured_output_model=PersonInfo)

    @workflow.run
    async def run(self, prompt: str) -> PersonInfo:
        result = await self.agent.invoke_async(prompt)
        return result.structured_output
```

`TemporalModel.structured_output()` called directly is not supported — always go through `Agent(structured_output_model=...)`.

## Streaming

To forward model chunks to external consumers, pass `streaming_topic="..."` to `TemporalModel` and host a `WorkflowStream` on the workflow. Each `StreamEvent` is published on the named topic from inside the model activity; subscribers read via `WorkflowStreamClient`. Chunks are batched on `streaming_batch_interval` (default 100ms).

```python
MODEL = TemporalModel(streaming_topic="events")

# workflow
@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.stream = WorkflowStream()
        self.agent = Agent(model=MODEL)

# client
async for item in WorkflowStreamClient.create(client, workflow_id).subscribe(
    ["events"], result_type=StreamEvent,
):
    print(item.data)
```

## Tools

Decorate non-deterministic tools with `@activity.defn`, or if you're importing tools from `strands_tools`, wrap them in a thin async function. Then, register the activity on the worker via `Worker(activities=[...])` and pass it to the agent with `activity_as_tool(activity, **options)` along with any activity options (e.g. `start_to_close_timeout`):

```python
from strands_tools import current_time

@activity.defn
async def fetch_user(user_id: str) -> dict:
    ...

@activity.defn(name="current_time")
async def current_time_activity() -> str:
    return current_time.current_time()

# workflow
agent = Agent(tools=[
    activity_as_tool(fetch_user, start_to_close_timeout=timedelta(seconds=30)),
    activity_as_tool(current_time_activity, start_to_close_timeout=timedelta(seconds=15)),
])

# worker
Worker(
    ...,
    activities=[fetch_user, current_time_activity],
    plugins=[StrandsPlugin(model=MODEL)],
)
```

## MCP

Construct `TemporalMCPClient` once at module level and reference the same instance from both the plugin (which registers a per-server `{server}-call-tool` activity and connects at worker startup to discover tools) and `Agent(tools=[...])`:

```python
from mcp import StdioServerParameters, stdio_client
from temporalio.contrib.strands import TemporalMCPClient

ECHO = TemporalMCPClient(
    server="echo",
    transport_factory=lambda: stdio_client(
        StdioServerParameters(command="...", args=[...]),
    ),
    start_to_close_timeout=timedelta(seconds=30),
)

# workflow
agent = Agent(tools=[ECHO])

# worker
Worker(..., plugins=[StrandsPlugin(mcp_clients=[ECHO])])
```

The plugin connects to the MCP server once at worker startup to enumerate tools. The schema is frozen for the worker's lifetime; restart workers to pick up MCP-server changes. If the MCP server is unavailable at startup, the worker fails to start.
