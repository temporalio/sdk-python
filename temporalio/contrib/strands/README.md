# Strands Agents

⚠️ **This package is currently at an experimental release stage.** ⚠️

This Temporal [Plugin](https://docs.temporal.io/develop/plugins-guide) allows you to run [Strands Agents](https://strandsagents.com/) inside Temporal Workflows, routing model invocations, tool calls, and MCP tool calls through Temporal Activities for durable execution, Temporal-managed retries, and timeouts.

## Installation

```sh
uv add temporalio[strands-agents]
```

## Quickstart

`workflow.py` defines the workflow and runs the worker:

```python
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.worker import Worker


@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(start_to_close_timeout=timedelta(seconds=60))

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
        plugins=[StrandsPlugin()],
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

## Models

`StrandsPlugin(models=...)` takes a mapping of `name → factory`. Each factory is called lazily on first use (on the worker, outside the workflow sandbox) and the constructed model is cached for the worker's lifetime. `TemporalAgent(model="name", ...)` selects which factory to invoke and carries the activity options for that agent's model calls. If `models` is omitted, the plugin registers a single `BedrockModel()` factory under the name `"bedrock"`, matching Strands' own implicit default.

```python
from strands.models.anthropic import AnthropicModel
from strands.models.bedrock import BedrockModel

# workflow
@workflow.defn
class MultiModelWorkflow:
    def __init__(self) -> None:
        self.agent_a = TemporalAgent(
            model="claude",
            start_to_close_timeout=timedelta(seconds=60),
        )
        self.agent_b = TemporalAgent(
            model="bedrock",
            start_to_close_timeout=timedelta(seconds=60),
        )

# worker
Worker(..., plugins=[StrandsPlugin(models={
    "claude": lambda: AnthropicModel(client_args={"api_key": "..."}),
    "bedrock": lambda: BedrockModel(),
})])
```

Each `TemporalAgent` carries its own activity options (timeouts, retry policy, task queue, streaming topic) and dispatches to the shared model activity, which resolves the model name against the registered factories at runtime. A name not present in `models` raises `ValueError` inside the activity.

## Retries

`TemporalAgent` disables Strands' built-in `ModelRetryStrategy` so retries are handled exclusively by Temporal. Configure retries via `retry_policy` on `TemporalAgent`, and on the activity options accepted by `workflow.activity_as_tool`, `workflow.activity_as_hook`, and `TemporalMCPClient`:

```python
from temporalio.common import RetryPolicy

TemporalAgent(
    start_to_close_timeout=timedelta(seconds=60),
    retry_policy=RetryPolicy(maximum_attempts=3),
)
```

Passing `retry_strategy=...` to `TemporalAgent(...)` raises `ValueError`; remove the argument (or pass `retry_strategy=None`) and put the retry config on the activity options instead.

## Snapshots

`TemporalAgent.take_snapshot()` and `TemporalAgent.load_snapshot()` raise `NotImplementedError`. Temporal's event history already persists workflow state durably at a finer granularity than Strands snapshots, so calling either inside a workflow is redundant.

## Structured Output

Like Strands `Agent`, `TemporalAgent` supports structured output with `structured_output_model`. The plugin defaults to [`pydantic_data_converter`](../pydantic), so Pydantic types easily serialize across the activity and workflow boundary.

```python
from pydantic import BaseModel

class PersonInfo(BaseModel):
    name: str
    age: int

@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            start_to_close_timeout=timedelta(seconds=60),
            structured_output_model=PersonInfo,
        )

    @workflow.run
    async def run(self, prompt: str) -> PersonInfo:
        result = await self.agent.invoke_async(prompt)
        return result.structured_output
```

## Streaming

To forward model chunks to external consumers, pass `streaming_topic="..."` to `TemporalAgent` and host a `WorkflowStream` on the workflow. Each `StreamEvent` is published on the named topic from inside the model activity; subscribers read via `WorkflowStreamClient`. Chunks are batched on `streaming_batch_interval` (default 100ms).

```python
# workflow
@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.stream = WorkflowStream()
        self.agent = TemporalAgent(streaming_topic="events")

# client
async for item in WorkflowStreamClient.create(client, workflow_id).subscribe(
    ["events"], result_type=StreamEvent,
):
    print(item.data)
```

## Sandboxes

`TemporalSandbox` implements Strands' sandbox API by scheduling every command,
code, and filesystem operation as a Temporal Activity. Register the real
worker-side sandbox under a name, then select that name in workflow code:

```python
from strands.sandbox.docker import DockerSandbox
from temporalio.contrib.strands import (
    SandboxWorkflowContext,
    StrandsPlugin,
    TemporalAgent,
    TemporalSandbox,
)

async def build_sandbox(context: SandboxWorkflowContext) -> DockerSandbox:
    # Application-specific and idempotent: return the existing container when
    # another activity worker has already provisioned this Workflow's sandbox.
    container = await get_or_create_build_container(
        context.chain.first_execution_run_id
    )
    return DockerSandbox(container.name)

# workflow
agent = TemporalAgent(
    sandbox=TemporalSandbox(
        "build",
        start_to_close_timeout=timedelta(minutes=5),
    ),
)

# worker
Worker(
    ...,
    plugins=[StrandsPlugin(sandboxes={
        "build": build_sandbox,
    })],
)
```

The factory is called lazily with a `SandboxWorkflowContext` containing the
current `run_id` and a `chain` identity with the Workflow's namespace, Workflow
ID, and first execution Run ID. The worker-local cache uses the chain identity,
so each Workflow chain gets a separate sandbox for each registered name. Retries,
Continue-As-New, Reset, and Cron runs belong to the same chain and therefore use
the same sandbox; unrelated Workflow chains do not share one. Multiple
`TemporalSandbox` objects with the same name in one chain intentionally share
that chain's sandbox.

The factory receives the current Run ID only when a worker-local cache entry is
created. A later run in the same chain reuses a warm entry without calling the
factory again. After eviction, the next factory call receives the Run ID of the
run that recreates the entry.

Factories may be synchronous or asynchronous. Synchronous factories must only
construct a lightweight adapter and must not block the activity event loop;
use an asynchronous factory for remote lookup or provisioning. A factory may
run more than once for the same context after cache eviction or on different
workers, so provisioning must be idempotent. Strands' `DockerSandbox` only
connects to an already-running container; it does not create one.

Worker-local adapters are reused until they have been idle for five minutes.
Set `sandbox_cache_idle_timeout` on `StrandsPlugin` to change that duration.
Eviction only drops the local adapter. Provisioning, teardown, and cleanup of
orphaned backing environments remain the application's responsibility; use a
backend TTL or reaper for workflows that are terminated before normal cleanup.

That cache is per worker *process*, while successive sandbox activities from one
workflow are routed independently across the task queue. With more than one
worker on the queue, a `write-file` can land on one worker and the following
`read-file` on another. The context factory must therefore reconnect every
worker to the same Workflow-scoped backing environment rather than relying on
per-process state. A single worker on the queue also satisfies this.

Reset does not roll back commands or filesystem mutations already performed in
the external sandbox, just as it does not roll back other Activity side effects.
Account for that when resetting a Workflow that uses a sandbox.

`SandboxTimeoutError` and any `FileNotFoundError` — including its
`SandboxPathNotFoundError` subclass — cross the activity boundary as
non-retryable failures and are re-raised inside the workflow with the sandbox's
own message, so a command that exceeds its `timeout` or a path that does not
exist surfaces to the agent on the first attempt instead of retrying. Other
sandbox failures, including the `OSError` that Strands documents for a failed
`write_file`, are retried under the `retry_policy` you pass to
`TemporalSandbox`.

Like all Temporal Activities, sandbox operations have at-least-once execution
semantics. A worker can finish a command or filesystem mutation and fail before
recording its result, causing a retry to perform the operation again. Use a
bounded `retry_policy`, and make commands and mutations idempotent when repeated
execution would be unsafe.

By default, `TemporalSandbox.get_tools()` vends `sandbox_bash` and
`sandbox_file_editor`. A tool passed explicitly through `TemporalAgent(tools=...)`
with either name takes precedence, following Strands' normal sandbox-tool
override behavior.

Execution output is always buffered into the activity result so workflow replay
observes the same ordered `StreamChunk` and `ExecutionResult` values. For live,
observer-facing output, set `streaming_topic` and host a `WorkflowStream` on the
workflow. The activity publishes each `StreamChunk` as it arrives; the final
`ExecutionResult` is returned only through the buffered activity result:

```python
from datetime import timedelta

from strands.sandbox import StreamChunk
from temporalio.contrib.strands import TemporalSandbox
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient

# workflow __init__
self.stream = WorkflowStream()
self.sandbox = TemporalSandbox(
    "build",
    start_to_close_timeout=timedelta(minutes=5),
    streaming_topic="sandbox-events",
)

# external client
async for item in WorkflowStreamClient.create(client, workflow_id).subscribe(
    ["sandbox-events"], result_type=StreamChunk
):
    print(item.data.stream_type, item.data.data)
```

The topic is an observer-facing merged log. If sandbox executions overlap,
their chunks may interleave. Use different `streaming_topic` values when the
consumer needs separate logs; workflow code still receives the correctly
separated, complete buffered result for each call. Because publications are
observer-facing side effects of an activity attempt, a failed attempt that
Temporal retries may leave chunks in the topic before the retry publishes its
own output.

Streaming is disabled by default. When `streaming_topic=None`, sandbox
activities do not construct a `WorkflowStreamClient` and the workflow does not
need to host a `WorkflowStream`.

All arguments and results cross Temporal's payload boundary and enter workflow
history. Keep command output and files within the server's configured payload
size limits; use external storage for large artifacts. In particular, `env`
values are recorded in history and must not contain secrets.

## Tools

Decorate non-deterministic tools with `@activity.defn`, or if you're importing tools from `strands_tools`, wrap them in a thin async function. Then, register the activity on the worker via `Worker(activities=[...])` and pass it to the agent with `workflow.activity_as_tool(activity, **options)` along with any activity options (e.g. `start_to_close_timeout`):

```python
from strands_tools import shell
from temporalio.contrib.strands import workflow as strands_workflow

@activity.defn
async def fetch_user(user_id: str) -> dict:
    ...

@activity.defn(name="shell")
async def shell_activity(command: str) -> dict:
    return shell.shell(command=command, non_interactive=True)

# workflow
agent = TemporalAgent(
    start_to_close_timeout=timedelta(seconds=60),
    tools=[
        strands_workflow.activity_as_tool(fetch_user, start_to_close_timeout=timedelta(seconds=30)),
        strands_workflow.activity_as_tool(shell_activity, start_to_close_timeout=timedelta(seconds=15)),
    ],
)

# worker
Worker(
    ...,
    activities=[fetch_user, shell_activity],
    plugins=[StrandsPlugin(models=MODELS)],
)
```

## Hooks

Strands' [hook system](https://strandsagents.com/) (`strands.hooks`) lets you subscribe callbacks to events in the agent lifecycle — invocation start/end, model call before/after, tool call before/after, message added. Pass `hooks=[MyHookProvider()]` to `TemporalAgent`: every single-agent hook event fires in workflow context, so deterministic callbacks just work.

```python
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

class AuditHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_tool_call)

    def _on_tool_call(self, event: AfterToolCallEvent) -> None:
        # Pure local state - deterministic across replay.
        workflow.logger.info(f"tool {event.tool_use['name']} finished")

agent = TemporalAgent(start_to_close_timeout=..., hooks=[AuditHook()])
```

Callbacks run in workflow context, so they must be deterministic: no `time.time()`, `uuid.uuid4()`, or I/O — same rules as workflow code. For callbacks that need I/O (audit logging, metrics, alerting), use `workflow.activity_as_hook()` to dispatch the work as a Temporal activity:

```python
from temporalio.contrib.strands.workflow import activity_as_hook

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

Strands offers two HITL surfaces; both work with the plugin. In each case, `agent.invoke_async()` returns `AgentResult(stop_reason="interrupt", interrupts=[...])` instead of raising. Pair this with a signal handler that supplies responses, then resume by calling `agent.invoke_async(responses)`.

### Hook-based interrupts

A hook on an interruptible event (e.g. `BeforeToolCallEvent`) can pause the agent by calling `event.interrupt(name, reason=...)`. The hook runs in workflow context, so it must be deterministic — no I/O.

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
        self.agent = TemporalAgent(
            start_to_close_timeout=timedelta(seconds=60),
            tools=[delete_thing],
            hooks=[ApprovalHook()],
        )
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

### Tool-body interrupts

A `@strands.tool` function can raise `InterruptException(Interrupt(...))` directly. The agent stops with the interrupt, the workflow handles the resume the same way as for hooks.

```python
from strands import tool
from strands.interrupt import Interrupt, InterruptException

@tool
def delete_thing(name: str) -> str:
    raise InterruptException(
        Interrupt(id=f"delete:{name}", name="approval", reason=f"delete {name}?")
    )
```

The same works from an `activity_as_tool`-wrapped activity. The plugin's failure converter preserves the `Interrupt` payload across the activity boundary, so `AgentResult.interrupts` is populated just like the in-workflow case:

```python
from strands.interrupt import Interrupt, InterruptException
from temporalio.contrib.strands.workflow import activity_as_tool

@activity.defn
async def delete_thing(name: str) -> str:
    if not await policy.is_authorized(name):
        raise InterruptException(
            Interrupt(id=f"delete:{name}", name="approval", reason=f"delete {name}?")
        )
    await storage.delete(name)
    return f"deleted {name}"

@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            start_to_close_timeout=timedelta(seconds=60),
            tools=[activity_as_tool(delete_thing, start_to_close_timeout=timedelta(seconds=10))],
        )
```

This relies on the plugin's failure converter, which is installed via the client's data converter. **Attach `StrandsPlugin` to the client** (not just the worker) for activity-tool interrupts to work — workers built from that client pick up the plugin automatically.

```python
client = await Client.connect("localhost:7233", plugins=[StrandsPlugin(models=MODELS)])
Worker(client, task_queue="strands", workflows=[MyWorkflow], activities=[delete_thing])
```

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
        agent = TemporalAgent(
            start_to_close_timeout=timedelta(seconds=60),
            messages=list(input.messages),
        )
        while True:
            await workflow.wait_condition(lambda: self._pending or self._done)
            if self._done:
                return
            await agent.invoke_async(self._pending.pop(0))
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(ChatInput(messages=agent.messages))
```

## MCP

`StrandsPlugin(mcp_clients=...)` takes a mapping of `name → MCPClient factory`, mirroring the `models=` pattern. The plugin registers per-server `{name}-call-tool` and `{name}-list-tools` activities. Workflow-side, `TemporalMCPClient(server="name")` is a pure handle: it references the server by name, discovers tools by running `{name}-list-tools`, and carries the per-call activity options.

```python
from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp.mcp_client import MCPClient
from temporalio.contrib.strands import TemporalMCPClient

# workflow
@workflow.defn
class MyWorkflow:
    def __init__(self) -> None:
        echo = TemporalMCPClient(server="echo", start_to_close_timeout=timedelta(seconds=30))
        self.agent = TemporalAgent(
            start_to_close_timeout=timedelta(seconds=60),
            tools=[echo],
        )

# worker
Worker(
    ...,
    plugins=[StrandsPlugin(
        mcp_clients={
            "echo": lambda: MCPClient(
                lambda: stdio_client(
                    StdioServerParameters(command="...", args=[...]),
                ),
            ),
        },
    )],
)
```

Each factory returns a fully configured `MCPClient`, so you can pass options like `tool_filters`, `prefix`, `elicitation_callback`, or `tasks_config` to it.

By default, `TemporalMCPClient` re-lists the server's tools (via `{name}-list-tools`) on every agent turn, so an MCP server that is restarted mid-workflow — with tools added, removed, or renamed — is picked up. To list the tools just once at the beginning of the workflow and reuse that schema for the workflow's lifetime (one fewer activity per turn), set `cache_tools=True`:

```python
echo = TemporalMCPClient(server="echo", cache_tools=True, start_to_close_timeout=timedelta(seconds=30))
```

To amortize connection setup, the `{name}-call-tool` and `{name}-list-tools` activities share a worker-process MCP connection that is opened lazily and reused across calls. The connection is disconnected after it sits idle for `mcp_connection_idle_timeout` (default 5 minutes); the timer resets on every reuse:

```python
StrandsPlugin(
    mcp_clients={"echo": lambda: MCPClient(...)},
    mcp_connection_idle_timeout=timedelta(seconds=30),
)
```

## Observability

`StrandsPlugin` composes cleanly with [`OpenTelemetryPlugin`](../opentelemetry). Register `OpenTelemetryPlugin` on the client (workers built from that client pick it up automatically) and `StrandsPlugin` on the worker. You'll get OTel spans around the model, tool, and MCP activities the plugin schedules, plus any spans Strands itself emits inside `invoke_async`:

```python
import opentelemetry.trace
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider

opentelemetry.trace.set_tracer_provider(create_tracer_provider())

client = await Client.connect("localhost:7233", plugins=[OpenTelemetryPlugin()])

Worker(
    client,
    task_queue="strands",
    workflows=[MyWorkflow],
    plugins=[StrandsPlugin(models=MODELS)],
)
```

Set the tracer provider before connecting the client. See the [OpenTelemetry plugin README](../opentelemetry) for exporter setup.
