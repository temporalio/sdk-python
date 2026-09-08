# DeepAgentsPlugin — Temporal plugin for LangChain Deep Agents

Make a [Deep Agent](https://github.com/langchain-ai/deepagents) durable by adding
one plugin. Build your agent with `create_temporal_deep_agent(...)` (or vanilla
`create_deep_agent(...)`) inside a `@workflow.defn`, add
`plugins=[DeepAgentsPlugin(...)]` to your Client (or Worker), and each LLM call
and each I/O tool call becomes a Temporal Activity — while the agent's control
loop runs, and deterministically replays, inside the Workflow.

The code you already wrote against `deepagents` does not change: sub-agents,
planning/todo state, the filesystem middleware, human-in-the-loop interrupts, and
`agent.ainvoke(...)` all keep working. You get crash-durability, resumable
human-in-the-loop, and bounded history on top.

> This package is experimental and may change in future versions.

## Install

```bash
uv add "temporalio[deepagents]"
```

(or `pip install "temporalio[deepagents]"`). Requires Python ≥ 3.11 (the same
floor `deepagents` sets).

## Hello world

```python
import asyncio
from datetime import timedelta

from deepagents import create_deep_agent  # no import guard needed; see below
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.deepagents import (
    DeepAgentsPlugin,
    create_temporal_deep_agent,
)
from temporalio.worker import Worker


@workflow.defn
class ResearchAgent:
    @workflow.run
    async def run(self, question: str) -> str:
        # create_temporal_deep_agent wraps deepagents' create_deep_agent and
        # scopes this agent's model-call activity options explicitly.
        agent = create_temporal_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            system_prompt="You are a careful research assistant.",
            activity_options={"start_to_close_timeout": timedelta(minutes=5)},
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        return result["messages"][-1].content


async def main() -> None:
    # API keys live on the worker via the model provider, never in workflow
    # inputs or history. The default provider is LangChain's init_chat_model.
    plugin = DeepAgentsPlugin()
    # Add the plugin on ONE side. The SDK propagates a Client plugin to any
    # Worker built from that Client, so the Worker below inherits it.
    client = await Client.connect("localhost:7233", plugins=[plugin])
    worker = Worker(
        client,
        task_queue="deepagents-task-queue",
        workflows=[ResearchAgent],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
```

Two things worth noticing:

- **No `workflow.unsafe.imports_passed_through()` guard.** The plugin
  configures the workflow sandbox to pass the `deepagents` / LangChain import
  tree through, so workflow files import them like any other module.
- **Vanilla `create_deep_agent(...)` also works.** The plugin substitutes the
  durable model automatically whenever `model=` is a name string; use
  `create_temporal_deep_agent` when you want to scope `activity_options` to one
  agent instead of configuring plugin-wide defaults.

## What this plugin gives you

- **Drop-in durability.** `create_deep_agent(...).ainvoke(...)` runs unchanged
  inside a Workflow. The loop replays deterministically; every nondeterministic
  step (LLM, I/O tool, real filesystem/shell op) is an Activity.
- **One LLM call per Activity.** The Workflow ships only the model *name*; the
  worker's `model_provider` builds the real client. Temporal owns retries and
  timeouts (LLM-SDK retries are disabled).
- **Sub-agents inherit durability.** Because sub-agents inherit the parent's
  `model` object and tools, substituting them once propagates to the whole agent
  tree — no per-sub-agent wiring.
- Tools, real-I/O backends, human-in-the-loop, streaming, and continue-as-new
  each get a section below.

## Configuring activity options

Per agent (recommended): `create_temporal_deep_agent(..., activity_options=...)`
as in Hello world above. Plugin-wide defaults use two keyed maps, because model
calls and tool calls have different timeout profiles:

```python
from datetime import timedelta

from temporalio.contrib.deepagents import DeepAgentsPlugin

plugin = DeepAgentsPlugin(
    # A single config, or a map keyed by MODEL name (thinking-mode models get
    # longer timeouts than fast ones).
    model_activity_options={"start_to_close_timeout": timedelta(minutes=5)},
    # A single config, or a map keyed by TOOL name.
    tool_activity_options={"start_to_close_timeout": timedelta(seconds=30)},
)
```

## Tools: the Workflow-vs-Activity choice, made explicit

A tool that only mutates agent state can run in-workflow; a tool that does real
I/O must not. Both directions are one call:

```python
from datetime import timedelta

from langchain_core.tools import tool
from temporalio import activity
from temporalio.contrib.deepagents import activity_as_tool, tool_as_activity


@activity.defn
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It is sunny and 22C in {city}."


@tool
def web_search(query: str) -> str:
    """Search the web for a query."""
    return f"Top result for {query!r}: ..."


# An existing Temporal activity, exposed to the agent as a tool:
weather_tool = activity_as_tool(
    get_weather, start_to_close_timeout=timedelta(seconds=30)
)
# A LangChain tool whose body does I/O, moved into an activity:
search_tool = tool_as_activity(
    web_search, start_to_close_timeout=timedelta(seconds=30)
)
```

Pass both to `create_temporal_deep_agent(..., tools=[weather_tool,
search_tool])`. An unwrapped, non-builtin tool runs in-workflow and the plugin
warns at construction, so the choice is never silent. Deep Agents' pure
built-ins (`write_todos`, state-backed file tools) stay in-workflow by design.

## Durable file and shell backends

Wrap a real-I/O backend (`FilesystemBackend` / `LocalShellBackend` /
`StoreBackend`) in `TemporalBackend` and the agent's *built-in* file and shell
tools execute as durable `deepagents.backend_op` Activities instead of touching
disk from workflow code:

```python
from datetime import timedelta

from deepagents.backends import FilesystemBackend
from temporalio import workflow
from temporalio.contrib.deepagents import TemporalBackend, create_temporal_deep_agent


@workflow.defn
class FilesystemAgent:
    @workflow.run
    async def run(self, root_dir: str) -> str:
        backend = TemporalBackend(
            FilesystemBackend(root_dir=root_dir, virtual_mode=True),
            activity_options={"start_to_close_timeout": timedelta(seconds=30)},
        )
        agent = create_temporal_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            backend=backend,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Take notes as you work."}]}
        )
        return result["messages"][-1].content
```

State-only backends (the default) need no wrapping — they are pure workflow
state, replayed deterministically.

## Human-in-the-loop

With `interrupt_on=...`, the agent pauses before a guarded tool and
`ainvoke(...)` returns the pending approval under the SDK-native
`__interrupt__` key — directly in your workflow. Expose it via a Query and
resume with an Update; no shim exception, the native LangGraph resume protocol
is used as-is:

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from temporalio import workflow
from temporalio.contrib.deepagents import create_temporal_deep_agent


@workflow.defn
class ApprovalAgent:
    def __init__(self) -> None:
        self._pending: str | None = None
        self._decision: str | None = None

    @workflow.run
    async def run(self, request: str) -> str:
        agent = create_temporal_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            interrupt_on={"book_trip": True},
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": workflow.info().workflow_id}}
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": request}]}, config=config
        )
        if result.get("__interrupt__"):
            self._pending = str(result["__interrupt__"][0].value)
            await workflow.wait_condition(lambda: self._decision is not None)
            result = await agent.ainvoke(
                Command(resume={"decisions": [{"type": self._decision}]}),
                config=config,
            )
        return result["messages"][-1].content

    @workflow.query
    def pending_approval(self) -> str | None:
        return self._pending

    @workflow.update
    async def resume(self, decision: str) -> None:
        self._decision = decision
```

A client polls `pending_approval`, shows it to a person, and calls the
`resume` update with `"approve"` / `"reject"`.

## Streaming

Set `streaming_topic=` on the plugin and model dispatch switches to a streaming
Activity that publishes chunk batches to a
`temporalio.contrib.workflow_streams` topic for live subscribers — while the
aggregated final message still returns to the workflow, so the durable result
is identical to the non-streaming path:

```python
from temporalio.contrib.deepagents import DeepAgentsPlugin

plugin = DeepAgentsPlugin(streaming_topic="agent-stream")
```

Subscribers read the topic with `WorkflowStreamClient`; each item is an
`AIMessageChunk` in `langchain_core.load.dumpd` form.

## Continue-as-new: what carries and what does not

Long conversations bloat workflow history. `run_deep_agent(agent, input,
state_snapshot=...)` snapshots state and continues into a fresh run when the
turn ends with pending todos and the server recommends continuing
(`workflow.info().is_continue_as_new_suggested()`, the default and recommended
mode — it accounts for history length *and* size):

```python
from deepagents import create_deep_agent
from temporalio import workflow
from temporalio.contrib.deepagents import run_deep_agent


@workflow.defn
class LongResearchAgent:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> dict:
        agent = create_deep_agent(model="anthropic:claude-sonnet-4-5")
        return await run_deep_agent(
            agent,
            input,
            state_snapshot=state_snapshot,
        )
```

Pass `continue_as_new_after=N` instead to trigger on a fixed history-event
count.

- **Carries forward:** the accumulated messages and the model/tool result cache
  (so an LLM/tool call completed before the continue-as-new is *not* re-run
  after it). Your `@workflow.run` must accept `state_snapshot=None` as shown.
- **Does not carry forward:** anything held only in an in-memory checkpointer's
  own structures beyond the messages/todos snapshot. The default in-workflow
  `InMemorySaver` is rehydrated for free by deterministic replay; a durable
  checkpointer that does its own I/O is not replay-safe from inside a workflow,
  and the plugin warns if you pass one — prefer the snapshot + continue-as-new
  path above.

## Runtime behavior

While a worker built with this plugin is running, the plugin wraps
`deepagents.create_deep_agent` so a bare `model="provider:name"` string is
auto-routed through an Activity. The wrapper only rewrites arguments when called
*inside a workflow*, so importing `deepagents` on a plain client or activity
worker is unaffected, and the original function is restored when the worker
stops. If you would rather be explicit, use `create_temporal_deep_agent` or
pass `TemporalModel("provider:name")` yourself.

## Composing with other plugins

This plugin carries no tracing context of its own. For observability, compose it
with `temporalio.contrib.langsmith` or `temporalio.contrib.opentelemetry` —
registration order does not matter:

```python
from temporalio.client import Client
from temporalio.contrib.deepagents import DeepAgentsPlugin


async def connect():
    return await Client.connect(
        "localhost:7233",
        plugins=[
            # LangSmithPlugin(),  # or OpenTelemetryPlugin(), in either order
            DeepAgentsPlugin(),
        ],
    )
```

For agents built directly as LangGraph graphs (rather than a compiled Deep
Agent), see `temporalio.contrib.langgraph`.
