# Strands Agents

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

## Hooks

Strands' [hook system](https://strandsagents.com/) (`strands.hooks`) lets you subscribe callbacks to events in the agent lifecycle — invocation start/end, model call before/after, tool call before/after, message added. The native `Agent(hooks=[MyHookProvider()])` API works as-is: every single-agent hook event fires in workflow context, so deterministic callbacks just work.

```python
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

class AuditHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_tool_call)

    def _on_tool_call(self, event: AfterToolCallEvent) -> None:
        # Pure local state - deterministic across replay.
        workflow.logger.info(f"tool {event.tool_use['name']} finished")

agent = Agent(hooks=[AuditHook()])
```

Callbacks run in workflow context, so they must be deterministic: no `time.time()`, `uuid.uuid4()`, or I/O — same rules as workflow code. For callbacks that need I/O (audit logging, metrics, alerting), use `activity_as_hook()` to dispatch the work as a Temporal activity:

```python
from temporalio.contrib.strands import activity_as_hook

@activity.defn
async def persist_tool_call(tool_name: str) -> None:
    # I/O safely in an activity.
    ...

class AuditHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(
            AfterToolCallEvent,
            activity_as_hook(
                persist_tool_call,
                activity_input=lambda event: event.tool_use["name"],
                start_to_close_timeout=timedelta(seconds=10),
            ),
        )
```

`activity_input` extracts serializable values from the event to pass as the activity's input. Use a dataclass or Pydantic model for multiple values. This is needed because events hold references to the `Agent`, `AgentTool` instances, etc., none of which cross the activity boundary.

## Human-in-the-loop interrupts

A hook on an interruptible event (e.g. `BeforeToolCallEvent`) can pause the agent by calling `event.interrupt(name, reason=...)`. When this fires, `agent.invoke_async()` returns `AgentResult(stop_reason="interrupt", interrupts=[...])` instead of raising. Pair this with a signal handler that supplies responses, then resume by calling `agent.invoke_async(responses)`:

```python
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

class ApprovalHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        if event.interrupt("approval", reason="confirm delete") != "approve":
            event.cancel_tool = "denied"

@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = Agent(model=MODEL, tools=[delete_thing], hooks=[ApprovalHook()])
        self._approval: str | None = None

    @workflow.signal
    def approve(self, response: str) -> None:
        self._approval = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        if result.stop_reason == "interrupt":
            await workflow.wait_condition(lambda: self._approval is not None)
            result = await self.agent.invoke_async([
                {"interruptResponse": {"interruptId": result.interrupts[0].id, "response": self._approval}}
            ])
        return str(result)
```

Interrupt hooks must be deterministic: branch on the activity result and call `event.interrupt(...)` on the workflow side. Tools wrapped via `activity_as_tool` cannot raise interrupts — the activity body has no `Agent` reference — so hooks are the interrupt surface for this plugin.

## Continue-as-new

A chat-style workflow accumulates history with every turn and will eventually hit Temporal's per-workflow history limit. `workflow.info().is_continue_as_new_suggested()` flips true once the server decides history has grown large enough; check it after each turn and hand off to a fresh run, carrying `agent.messages` as input:

```python
from dataclasses import dataclass, field
from strands.types.content import Messages

@dataclass
class ChatInput:
    messages: Messages = field(default_factory=list)

@workflow.defn
class ChatWorkflow:
    def __init__(self) -> None:
        self._pending: list[str] = []
        self._done = False

    @workflow.signal
    def user_says(self, prompt: str) -> None:
        self._pending.append(prompt)

    @workflow.signal
    def end_chat(self) -> None:
        self._done = True

    @workflow.run
    async def run(self, input: ChatInput) -> None:
        agent = Agent(model=MODEL, messages=list(input.messages))
        while True:
            await workflow.wait_condition(lambda: self._pending or self._done)
            if self._done:
                return
            await agent.invoke_async(self._pending.pop(0))
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(ChatInput(messages=agent.messages))
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
