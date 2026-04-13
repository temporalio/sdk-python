# temporalio[tool-registry]

LLM tool-calling primitives for Temporal activities — define tools once, use them with
Anthropic or OpenAI.

## Before you start

A Temporal Activity is a function that Temporal monitors and retries automatically on failure. Temporal streams progress between retries via heartbeats — that's the mechanism `agentic_session` uses to resume a crashed LLM conversation mid-turn.

`run_tool_loop` works standalone in any async function — no Temporal server needed. Add `agentic_session` only when you need crash-safe resume inside a Temporal activity.

`agentic_session` requires a running Temporal worker — it reads and writes heartbeat state from the active activity context. Use `run_tool_loop` standalone for scripts, one-off jobs, or any code that runs outside a Temporal worker.

New to Temporal? → https://docs.temporal.io/develop

## Install

```bash
pip install "temporalio[tool-registry]"           # Anthropic only
pip install "temporalio[tool-registry-openai]"    # Anthropic + OpenAI
```

## Quickstart

Tool definitions use [JSON Schema](https://json-schema.org/understanding-json-schema/) for `input_schema`. The quickstart uses a single string field; for richer schemas refer to the JSON Schema docs.

```python
from temporalio import activity
from temporalio.contrib.tool_registry import ToolRegistry, run_tool_loop

@activity.defn  # Remove for standalone use — no worker needed
async def analyze(prompt: str) -> list[str]:
    results: list[str] = []
    tools = ToolRegistry()

    @tools.handler({
        "name": "flag_issue",
        "description": "Flag a problem found in the analysis",
        "input_schema": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    })
    def handle_flag(inp: dict) -> str:
        results.append(inp["description"])
        return "recorded"  # this string is sent back to the LLM as the tool result

    await run_tool_loop(
        provider="anthropic",          # reads ANTHROPIC_API_KEY from environment; or use "openai"
        system="You are a code reviewer. Call flag_issue for each problem you find.",
        prompt=prompt,
        tools=tools,
    )
    return results
```

## Feature matrix

| Feature | `tool_registry` | `openai_agents` |
|---|---|---|
| Anthropic (claude-*) | ✓ | ✗ |
| OpenAI (gpt-*) | ✓ | ✓ |
| MCP tool wrapping | ✓ | ✓ |
| Crash-safe heartbeat resume | ✓ (via `agentic_session`) | ✗ |
| Agent orchestration (handoffs, etc.) | ✗ | ✓ |

Use `openai_agents`, `google_adk_agents`, or `langgraph` when you are already building with those frameworks and want each model call to be a separately observable, retryable Temporal activity.
Use `tool_registry` for direct Anthropic support, crash-safe sessions that survive server-side session expiry, or when you need the same implementation pattern across all six Temporal SDKs (Go, Java, Ruby, .NET have no framework-level integrations).

## Sandbox passthrough

You need this if you register both workflows and activities on the same `Worker` instance. If your activities run on a dedicated worker (no workflows registered), skip this section.

The Temporal workflow sandbox blocks third-party imports.  If your activity
worker runs alongside a sandboxed workflow worker, use `ToolRegistryPlugin`:

```python
from temporalio.contrib.tool_registry import ToolRegistryPlugin
from temporalio.worker import Worker

worker = Worker(
    client,
    task_queue="my-queue",
    plugins=[ToolRegistryPlugin(provider="anthropic")],
    workflows=[MyWorkflow],
    activities=[analyze],
)
```

## MCP integration

MCP tool wrapping is supported via `ToolRegistry.from_mcp_tools()`. See the MCP integration guide for a complete example including server setup.

### Selecting a model

The default model is `"claude-sonnet-4-6"` (Anthropic) or `"gpt-4o"` (OpenAI). Pass `model=` to `run_tool_loop`:

```python
await run_tool_loop(
    provider="anthropic",
    model="claude-3-5-sonnet-20241022",
    system="...",
    prompt=prompt,
    tools=tools,
)
```

Model IDs are defined by the provider — see Anthropic or OpenAI docs for current names.

### OpenAI

```python
await run_tool_loop(
    provider="openai",  # reads OPENAI_API_KEY from environment
    system="...",
    prompt=prompt,
    tools=tools,
)
```

## Crash-safe agentic sessions

For multi-turn LLM conversations that must survive activity retries, use
`agentic_session`.  It saves conversation history via `activity.heartbeat()`
on every turn and restores it automatically on retry.

```python
from temporalio.contrib.tool_registry import ToolRegistry, agentic_session

@activity.defn
async def long_analysis(prompt: str) -> list[str]:
    async with agentic_session() as session:
        tools = ToolRegistry()

        @tools.handler({"name": "flag", "description": "...", "input_schema": {"type": "object"}})
        def handle_flag(inp: dict) -> str:
            session.results.append(inp)
            return "ok"  # this string is sent back to the LLM as the tool result

        await session.run_tool_loop(
            registry=tools,
            provider="anthropic",
            system="...",
            prompt=prompt,
        )
    return session.results
```

## Human-in-the-loop tool calls

A tool handler can block waiting for a human decision before returning a result to the
LLM. Because conversation state is stored in the heartbeat — not in a provider-side
session — the activity can wait hours without losing context. Framework plugins that rely
on API session IDs cannot do this: those sessions expire.

The pattern: the handler starts a Temporal workflow that notifies a reviewer, then blocks
on its result. The human signals the workflow to approve or reject. If the activity
crashes while waiting, the next retry re-attaches to the same workflow via a deterministic
ID — no duplicate notifications, no lost decisions.

The rejection reason is returned to the LLM as the tool result. The model can read it and
revise its next proposal accordingly.

```python
import asyncio
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowIDConflictPolicy
from temporalio.contrib.tool_registry import ToolRegistry, agentic_session


# ── Approval workflow ──────────────────────────────────────────────────────────

@workflow.defn
class FixApprovalWorkflow:
    """Waits for a human to approve or reject a proposed code fix."""

    def __init__(self) -> None:
        self._decision: dict | None = None

    @workflow.run
    async def run(self, fix: dict) -> dict:
        # Notify reviewer here (Slack, email, etc.) using workflow.execute_activity
        await workflow.wait_condition(
            lambda: self._decision is not None,
            timeout=timedelta(hours=24),
        )
        return self._decision or {"approved": False, "reason": "timed out"}

    @workflow.signal
    def decide(self, decision: dict) -> None:
        self._decision = decision


# ── Activity ───────────────────────────────────────────────────────────────────

@activity.defn
async def review_and_fix(diff: str) -> list[dict]:
    """Review a code diff; each proposed fix requires human sign-off."""

    async with agentic_session() as session:
        tools = ToolRegistry()

        @tools.handler({
            "name": "propose_fix",
            "description": (
                "Propose a code fix requiring human approval. "
                "Returns 'approved' or 'rejected: <reason>'. "
                "If rejected, revise your approach using the stated reason."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "description": {"type": "string"},
                    "patch": {"type": "string", "description": "Unified diff to apply"},
                },
                "required": ["file", "description", "patch"],
            },
        })
        async def handle_propose_fix(inp: dict) -> str:
            client = await Client.connect("localhost:7233")

            # Deterministic ID: crash-retry re-attaches to the existing workflow
            # rather than starting a duplicate. If the human already decided before
            # the crash, handle.result() returns immediately.
            wf_id = f"fix-{activity.info().activity_id}-{inp['file']}"
            handle = await client.start_workflow(
                FixApprovalWorkflow.run,
                inp,
                id=wf_id,
                task_queue="approvals",
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )

            # agentic_session heartbeats before each LLM turn, not during tool
            # execution. Heartbeat manually here so the activity isn't timed out
            # while waiting for a human reviewer.
            async def _heartbeat() -> None:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(10)

            hb = asyncio.create_task(_heartbeat())
            try:
                decision = await handle.result()
            finally:
                hb.cancel()

            if decision["approved"]:
                session.results.append(inp)
                return "approved — fix applied"
            return f"rejected: {decision.get('reason', 'no reason given')}"

        await session.run_tool_loop(
            registry=tools,
            provider="anthropic",
            system=(
                "You are a code reviewer. Propose fixes using propose_fix. "
                "If a fix is rejected, revise your approach using the stated reason."
            ),
            prompt=f"Review this diff and propose fixes:\n\n{diff}",
        )
    return session.results
```

Reviewer signals approval from any Temporal client:

```python
handle = client.get_workflow_handle(wf_id)
await handle.signal(FixApprovalWorkflow.decide, {"approved": True})
# or with a rejection reason the LLM will read:
await handle.signal(FixApprovalWorkflow.decide, {"approved": False, "reason": "scope too broad — fix one thing at a time"})
```

## Testing without an API key

```python
from temporalio.contrib.tool_registry import ToolRegistry
from temporalio.contrib.tool_registry.testing import MockProvider, ResponseBuilder

tools = ToolRegistry()

@tools.handler({"name": "flag", "description": "d", "input_schema": {"type": "object"}})
def handle_flag(inp: dict) -> str:
    return "ok"  # this string is sent back to the LLM as the tool result

provider = MockProvider([
    ResponseBuilder.tool_call("flag", {"description": "stale API"}),
    ResponseBuilder.done("done"),
])
messages = [{"role": "user", "content": "analyze"}]
provider.run_loop(messages, tools)  # synchronous
assert len(messages) > 2
```

## Integration testing with real providers

To run the integration tests against live Anthropic and OpenAI APIs:

```bash
RUN_INTEGRATION_TESTS=1 \
  ANTHROPIC_API_KEY=sk-ant-... \
  OPENAI_API_KEY=sk-proj-... \
  uv run pytest tests/contrib/tool_registry/ -v
```

Tests skip automatically when `RUN_INTEGRATION_TESTS` is unset. Real API calls
incur billing — expect a few cents per full test run.

## Storing application results

`session.results` accumulates application-level results during the tool loop.
Elements are serialized to JSON inside each heartbeat checkpoint — they must be
plain maps/dicts with JSON-serializable values. A non-serializable value raises
a non-retryable `ApplicationError` at heartbeat time rather than silently losing
data on the next retry.

### Storing typed results

Convert your domain type to a plain dict at the tool-call site and back after
the session:

```python
import dataclasses

@dataclasses.dataclass
class Finding:
    type: str
    file: str

# Inside tool handler:
session.results.append(dataclasses.asdict(Finding(type="smell", file="foo.py")))

# After session:
findings = [Finding(**r) for r in session.results]
```

## Per-turn LLM timeout

Individual LLM calls inside the tool loop are unbounded by default. A hung HTTP
connection holds the activity open until Temporal's `ScheduleToCloseTimeout`
fires — potentially many minutes. Set a per-turn timeout on the provider client:

```python
import anthropic
client = anthropic.Anthropic(api_key=..., timeout=30.0)
await session.run_tool_loop(..., client=client)
```

Recommended timeouts:

| Model type | Recommended |
|---|---|
| Standard (Claude 3.x, GPT-4o) | 30 s |
| Reasoning (o1, o3, extended thinking) | 300 s |

### Activity-level timeout

Set `schedule_to_close_timeout` on the activity options to bound the entire conversation:

```python
await workflow.execute_activity(
    long_analysis,
    prompt,
    schedule_to_close_timeout=timedelta(seconds=600),
)
```

The per-turn client timeout and `schedule_to_close_timeout` are complementary:
- Per-turn timeout fires if one LLM call hangs (protects against a single stuck turn)
- `schedule_to_close_timeout` bounds the entire conversation including all retries (protects against runaway multi-turn loops)
