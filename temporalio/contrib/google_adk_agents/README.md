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

## Integration Points

This integration provides comprehensive support for running Google ADK Agents within Temporal workflows while maintaining:
- **Determinism**: All non-deterministic operations are routed through Temporal
- **Observability**: Full tracing and activity visibility
- **Reliability**: Proper retry handling and error propagation  
- **Extensibility**: Support for custom tools via MCP protocol
