# Google ADK Agents SDK Integration for Temporal

This package provides the integration layer between the Google ADK and Temporal. It allows ADK Agents to run reliably within Temporal Workflows by ensuring determinism and correctly routing external calls (network I/O) through Temporal Activities.

## Benefits of Temporal to the ADK

Temporal provides a holistic, unified solution that centralizes your orchestration needs in one Workflow abstraction. Rather than cobbling together separate servers, task queues, gateways, and databases, you get:

- **Recovering from crashes and stalls automatically**, rather than manually managing [sessions](https://google.github.io/adk-docs/sessions/session/#example-examining-session-properties) and [resuming](https://google.github.io/adk-docs/runtime/resume/#resume-a-stopped-workflow) them. (Google offers [Vertex Agent Engine](https://docs.cloud.google.com/agent-builder/agent-engine/sessions/manage-sessions-adk), which still leaves resumption to the user). No need to set up a separate [database](https://dev.to/greyisheepai/mastering-google-adk-databasesessionservice-and-events-complete-guide-to-event-injection-and-pdm#understanding-adk-databasesessionservice)
    - Along with [Retries](https://docs.temporal.io/encyclopedia/retry-policies) and mechanisms for handling backpressure and rate limits.
- **Support for [ambient](https://temporal.io/blog/orchestrating-ambient-agents-with-temporal)/long-running agent patterns** via blocking awaits and [worker versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning).
- **Automatic execution state [persistence](https://docs.temporal.io/temporal-service/persistence)**, not just for agent interactions but for any custom automations in your workflows, without setting up a separate [database](https://dev.to/greyisheepai/mastering-google-adk-databasesessionservice-and-events-complete-guide-to-event-injection-and-pdm#understanding-adk-databasesessionservice).
- For **Human-in-the-Loop patterns,** an api gateway to scalably [route](https://docs.temporal.io/task-routing) incoming messages (such as user chats) to awaken the correct workflow on your worker pool.
- [**Long-running tools](https://google.github.io/adk-docs/tools-custom/function-tools/#long-run-tool) support** using [Activities](https://docs.temporal.io/activities) — no need to set up and maintain microservices.
- [Manage and debug your agent workflow](https://temporal.io/resources/on-demand/demo-ai-agent) execution and pinpoint problems using Temporal UI.

## Benefits of the ADK to Temporal

ADK provides: (from the [ADK overview](https://google.github.io/adk-docs/#learn-more)):

- Improved Agent development velocity with a first-class Agentic abstraction and integration with LLMs and an ecosystem of tools.
- Improved agent robustness using built-in evals
- Build complex agents using its Multi-agent architecture.
- [Safety and security](https://google.github.io/adk-docs/safety/), via guardrails and integrations with sandboxing solutions like Vertex Agent Runtime.

## What's Included

### Core ADK Integration
- **`TemporalModel`**: Intercepts model calls and executes them as Temporal activities
- **`GoogleAdkPlugin`**: Worker plugin that configures runtime determinism and Pydantic serialization
- **`invoke_model`**: Activity for executing LLM model calls with proper error handling

### MCP (Model Context Protocol) Integration  
- **`TemporalMcpToolSet`**: Executes MCP tools as Temporal activities
- **`TemporalMcpToolSetProvider`**: Manages toolset creation and activity registration
- Full support for tool confirmation and event actions within workflows

### OpenTelemetry Integration
- Automatic instrumentation for ADK components when exporters are provided
- Tracing integration that works within Temporal's execution context
- Support for custom span exporters

### Key Features

#### 1. Deterministic Runtime
- Replaces `time.time()` with `workflow.now()` when in workflow context
- Replaces `uuid.uuid4()` with `workflow.uuid4()` for deterministic IDs
- Automatic setup when using `GoogleAdkPlugin`

#### 2. Activity-Based Model Execution
Model calls are intercepted and executed as Temporal activities with configurable:
- Timeouts (schedule-to-close, start-to-close, heartbeat)
- Retry policies
- Task queues
- Cancellation behavior
- Priority levels

#### 3. Sandbox Compatibility
- Automatic passthrough for `google.adk`, `google.genai`, and `mcp` modules
- Works with both sandboxed and unsandboxed workflow runners

#### 4. Advanced Serialization
- Pydantic payload converter for ADK objects
- Proper handling of complex ADK data types
- Maintains type safety across workflow boundaries

## Usage

### Basic Setup

**Agent (Workflow) Side:**
```python
from temporalio.contrib.google_adk_agents import TemporalModel
from temporalio.workflow import ActivityConfig
from google.adk import Agent


# Add to agent
agent = Agent(
    name="test_agent",
    model=TemporalModel("gemini-2.5-pro", activity_config=ActivityConfig(summary="Researcher Agent")), 
)
```

**Worker Side:**

```python
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin

client = await Client.connect(
    "localhost:7233",
    plugins=[
        GoogleAdkPlugin(),
    ],
)

worker = Worker(
    client,
    task_queue="my-queue",
)
```

### Advanced Features

**With MCP Tools:**

```python
import os
from google.adk import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from temporalio.client import Client
from temporalio.worker import Worker

from temporalio.contrib.google_adk_agents import (
    GoogleAdkPlugin,
    TemporalMcpToolSetProvider,
    TemporalMcpToolSet,
)


def toolset_factory(_):
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=[
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    os.path.dirname(os.path.abspath(__file__)),
                ],
            ),
        ),
    )

# Use in agent workflow
agent = Agent(
    name="test_agent",
    model="gemini-2.5-pro",
    tools=[
        TemporalMcpToolSet(
            "my-tools",
            not_in_workflow_toolset=toolset_factory,
        )
    ],
)

client = await Client.connect(
    "localhost:7233",
    plugins=[
        GoogleAdkPlugin(
            toolset_providers=[
                TemporalMcpToolSetProvider("my-tools", toolset_factory),
            ],
        ),
    ],
)

# Configure worker
worker = Worker(
    client,
    task_queue="task-queue"
)
```

`TemporalMcpToolSet` also accepts an optional `factory_argument`. It is sent to the toolset activities and passed to the registered `toolset_factory` when the `McpToolset` is created.

**Do not pass secrets, credentials, or API keys through `factory_argument`.** It is an activity argument, so it is recorded in workflow history and, without a payload codec, visible in the web UI. Resolve credentials worker-side inside the toolset factory instead.

### Local ADK Runs

The same agent definitions can also be exercised outside Temporal with
`adk run` or `adk web`.

- `TemporalModel` and `activity_tool(...)` work in local ADK runs without
  additional configuration.
- If the agent uses `TemporalMcpToolSet`, define a shared toolset factory,
  register it with `TemporalMcpToolSetProvider(...)` for workflow runs, and
  reuse the same function for `not_in_workflow_toolset=...` so the agent can
  fall back to the underlying `McpToolset` when it is not running inside
  `workflow.in_workflow()`.

Example:

```python
# Reuse the same toolset_factory registered in GoogleAdkPlugin above.
agent = Agent(
    name="test_agent",
    model=TemporalModel("gemini-2.5-pro"),
    tools=[
        TemporalMcpToolSet(
            "my-tools",
            not_in_workflow_toolset=toolset_factory,
        )
    ],
)
```

## Graph Workflows (ADK v2)

ADK v2's graph runtime (`google.adk.workflow`) runs inside Temporal workflows:
the scheduler is pure asyncio and executes deterministically on Temporal's
workflow event loop, while LLM calls (`TemporalModel`), MCP tools
(`TemporalMcpToolSet`), and activity-backed nodes leave the workflow as
activities.

Use `activity_node(...)` to run a graph node as a Temporal activity. The
previous node's output is passed to the activity — directly for a
single-parameter activity, bound by name (from a dict) for multi-parameter
activities:

```python
from google.adk.workflow import JoinNode, Workflow
from temporalio.contrib.google_adk_agents.workflow import activity_node

fetch = activity_node(fetch_data, start_to_close_timeout=timedelta(seconds=30))

def summarize(node_input):  # plain nodes run in-workflow: keep deterministic
    return f"{node_input} summarized"

graph = Workflow(name="pipeline", edges=[("START", fetch, summarize)])
```

Conditional routing (`(router, {"KEY": handler, ...})`, `DEFAULT_ROUTE`),
parallel fan-out with `JoinNode`, and `LlmAgent` nodes (with
`mode="task"`/`"single_turn"`) all work — agent nodes route their model calls
through `TemporalModel` as usual.

## Dynamic Workflows

Dynamic nodes (`await ctx.run_node(...)` with loops, branches, and
`asyncio.gather`) work in-workflow; child-run caching reads only the
in-memory session, so re-entry after a HITL resume replays deterministically.

```python
from google.adk.workflow import node

@node(rerun_on_resume=True)
async def pipeline(ctx):
    data = await ctx.run_node(fetch, "query")          # activity_node child
    results = await asyncio.gather(
        *(ctx.run_node(worker, item) for item in data)  # parallel children
    )
    return results
```

On a HITL resume, a `rerun_on_resume=True` dynamic node re-executes its body
while completed children are skipped from the session cache. Place activity
invocations in child nodes (`activity_node`, `activity_tool`) rather than
inline in the dynamic node body, or make them idempotent — inline calls run
again on re-entry (ADK's documented at-least-once semantics).

A `Workflow` with an `input_schema` can also be passed in an agent's
`tools=[...]` list (Workflow-as-Tool), letting the model invoke whole graphs
as tools.

## Durable Human-in-the-Loop

ADK pauses a run for human input (a node yielding `RequestInput`) or tool
confirmation (`FunctionTool(..., require_confirmation=True)`); in a Temporal
workflow that pause becomes a durable wait. The
`pending_hitl_requests` / `hitl_input_response` / `hitl_confirmation_response`
helpers cover the wire format; the wait itself is ordinary workflow code:

```python
from temporalio.contrib.google_adk_agents import (
    HitlRequest,
    hitl_input_response,
    pending_hitl_requests,
)

@workflow.defn
class ApprovalWorkflow:
    def __init__(self) -> None:
        self._pending: dict[str, HitlRequest] = {}
        self._responses: dict[str, Any] = {}

    @workflow.query
    def pending_requests(self) -> list[HitlRequest]:
        return list(self._pending.values())

    @workflow.update
    def respond(self, interrupt_id: str, response: Any) -> None:
        self._responses[interrupt_id] = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        runner = Runner(
            app_name="app", node=graph, session_service=InMemorySessionService()
        )
        session = await runner.session_service.create_session(
            app_name="app", user_id="user"
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        result = ""
        while True:
            async for event in runner.run_async(
                user_id="user", session_id=session.id, new_message=message
            ):
                for request in pending_hitl_requests(event):
                    self._pending[request.interrupt_id] = request
                if event.content and event.content.parts and event.content.parts[0].text:
                    result = event.content.parts[0].text
            if not self._pending:
                return result
            await workflow.wait_condition(
                lambda: any(i in self._responses for i in self._pending)
            )
            parts = [
                hitl_input_response(i, self._responses.pop(i))
                for i in list(self._pending)
                if i in self._responses
            ]
            for part in parts:
                self._pending.pop(part.function_response.id)
            message = types.Content(role="user", parts=parts)
```

Tool confirmation composes with `activity_tool` with no extra plumbing —
`FunctionTool(func=activity_tool(risky_activity, ...), require_confirmation=True)`
never schedules the activity until the human approves (answer with
`hitl_confirmation_response(interrupt_id, confirmed=True)`). MCP tools
requesting confirmation via `tool_context.request_confirmation(...)` flow
through the same loop. Partial responses are fine: unanswered requests stay
pending across `run_async` turns.

> **Replay-safety note:** HITL resume matches recorded human responses against
> generated interrupt/function-call ids, so those ids must regenerate
> identically on replay. The plugin installs ADK's platform time/uuid/random
> providers as process-wide defaults to guarantee this. On google-adk versions
> where `RequestInput` ids bypass the platform seam, pass an explicit
> `interrupt_id` to `RequestInput(...)` (as the examples here do).

## Determinism Notes

- The plugin patches ADK's `google.adk.platform` time, uuid, and (on ADK
  versions that expose it) random providers to `workflow.now()`,
  `workflow.uuid4()`, and `workflow.random()` inside workflows.
- ADK node `timeout=`/`RetryConfig` map onto durable timers
  (`asyncio.wait_for`/`asyncio.sleep`). For activity-backed nodes, prefer
  Temporal activity timeouts and `retry_policy` via `activity_node(...)`
  options; an ADK `RetryConfig` on top would retry on top of Temporal's own
  activity retries, and an ADK node timeout cancels the in-flight activity.
- Never set `RunConfig.tool_thread_pool_config` inside a workflow — it runs
  tools on threads, which breaks workflow determinism. Live/BIDI mode is
  likewise unsupported in workflows.
- ADK resume is at-least-once: on a HITL resume, completed nodes fast-forward
  from the in-memory session, but `rerun_on_resume=True` node bodies
  re-execute. This is deterministic under Temporal replay; schedule side
  effects through activities (retried/tracked by Temporal) or make them
  idempotent.
- Very long HITL conversations grow the workflow history with each turn;
  consider `continue-as-new` boundaries between `run_async` turns for
  long-running chats.

## Integration Points

This integration provides comprehensive support for running Google ADK Agents within Temporal workflows while maintaining:
- **Determinism**: All non-deterministic operations are routed through Temporal
- **Observability**: Full tracing and activity visibility
- **Reliability**: Proper retry handling and error propagation  
- **Extensibility**: Support for custom tools via MCP protocol
