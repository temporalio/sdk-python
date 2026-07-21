# DeepAgentsPlugin — Temporal plugin for LangChain Deep Agents

Make a [Deep Agent](https://github.com/langchain-ai/deepagents) durable by adding
one plugin. Build your agent with `create_deep_agent(...)` inside a
`@workflow.defn`, add `plugins=[DeepAgentsPlugin(...)]` to your Client (or
Worker), and each LLM call and each I/O tool call becomes a Temporal Activity —
while the agent's control loop runs, and deterministically replays, inside the
Workflow.

The code you already wrote against `deepagents` does not change: sub-agents,
planning/todo state, the filesystem middleware, human-in-the-loop interrupts, and
`agent.ainvoke(...)` all keep working. You get crash-durability, resumable
human-in-the-loop, and bounded history on top.

> This package is experimental and may change in future versions.

## Install

```bash
pip install "temporalio[deepagents]"
```

Requires Python ≥ 3.11 (the same floor `deepagents` sets).

## Hello world

```python
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent

from temporalio.contrib.deepagents import DeepAgentsPlugin


@workflow.defn
class ResearchAgent:
    @workflow.run
    async def run(self, question: str) -> str:
        # Vanilla deepagents code. The plugin routes the model call through an
        # activity automatically because `model=` is a name string.
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            system_prompt="You are a careful research assistant.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        return result["messages"][-1].content


async def main() -> None:
    # API keys live on the worker via the model provider, never in workflow
    # inputs or history. The default provider is LangChain's init_chat_model.
    plugin = DeepAgentsPlugin(
        model_activity_options={"start_to_close_timeout": timedelta(minutes=5)},
    )
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

## What this plugin gives you

- **Drop-in durability.** `create_deep_agent(...).ainvoke(...)` runs unchanged
  inside a Workflow. The loop replays deterministically; every nondeterministic
  step (LLM, I/O tool, real filesystem/shell op) is an Activity.
- **One LLM call per Activity.** The Workflow ships only the model *name*; the
  worker's `model_provider` builds the real client. Temporal owns retries and
  timeouts (LLM-SDK retries are disabled), configurable per model via
  `model_activity_options`.
- **Existing activities as tools.** Already have `@activity.defn` functions?
  `activity_as_tool(my_activity, start_to_close_timeout=...)` exposes one to the
  agent without re-declaring it.
- **Explicit Workflow-vs-Activity choice per tool.** `tool_as_activity(tool, ...)`
  moves a LangChain tool's execution into an Activity. An unwrapped, non-builtin
  tool runs in-workflow and the plugin warns at construction so that choice is
  never silent. Deep Agents' pure built-ins (`write_todos`, state-backed file
  tools) stay in-workflow by design.
- **Real backends via activities.** Wrap a `FilesystemBackend` /
  `LocalShellBackend` / `StoreBackend` with `TemporalBackend(inner, ...)` so each
  file/shell op is a durable Activity. State-only backends need no wrapping.
- **Sub-agents inherit durability.** Because sub-agents inherit the parent's
  `model` object and tools, substituting them once propagates to the whole agent
  tree — no per-sub-agent wiring.
- **Human-in-the-loop.** With `interrupt_on=...` (and a checkpointer), the agent
  pauses and `ainvoke(...)` returns the pending approval under the SDK-native
  `__interrupt__` key directly in your workflow; expose it via a Query and resume
  with a Workflow Update carrying `Command(resume={"decisions": [...]})`. No shim
  exception — the native LangGraph resume protocol is used as-is.
- **Streaming.** Set `streaming_topic="..."` to stream chat-completion chunk
  batches out of the model Activity to external subscribers; the aggregated final
  message is returned to the workflow so the durable result is identical to the
  non-streaming path.

## Continue-as-new: what carries and what does not

Long conversations bloat workflow history. `run_deep_agent(agent, input,
continue_as_new_after=N)` snapshots state and continues into a fresh run once
history passes `N` events and the agent still has pending todos:

```python
from temporalio import workflow
from temporalio.contrib.deepagents import run_deep_agent

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent


@workflow.defn
class LongResearchAgent:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> dict:
        agent = create_deep_agent(model="anthropic:claude-sonnet-4-5")
        return await run_deep_agent(
            agent,
            input,
            continue_as_new_after=10_000,
            state_snapshot=state_snapshot,
        )
```

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
stops. If you would rather be explicit, pass `TemporalModel("provider:name")`
yourself.

## Composing with other plugins

This plugin carries no tracing context of its own. For observability, compose it
with `temporalio.contrib.langsmith` or `temporalio.contrib.opentelemetry`,
registered *before* this plugin:

```python
from temporalio.client import Client
from temporalio.contrib.deepagents import DeepAgentsPlugin


async def connect():
    return await Client.connect(
        "localhost:7233",
        plugins=[
            # ObservabilityPlugin(...),  # register observability first
            DeepAgentsPlugin(),
        ],
    )
```

For agents built directly as LangGraph graphs (rather than a compiled Deep
Agent), see `temporalio.contrib.langgraph`.
