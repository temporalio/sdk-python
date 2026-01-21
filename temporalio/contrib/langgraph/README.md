# Temporal LangGraph Integration

⚠️ **Experimental** - This module is experimental and may never advance to the next phase and may be abandoned.

## Introduction

This integration combines [LangGraph](https://github.com/langchain-ai/langgraph) with [Temporal's durable execution](https://docs.temporal.io/evaluate/understanding-temporal#durable-execution).
It allows you to build durable agents that never lose their progress and handle long-running, asynchronous, and human-in-the-loop workflows with production-grade reliability.

Temporal and LangGraph are complementary technologies.
Temporal provides a crash-proof system foundation, taking care of the distributed systems challenges inherent to production agentic systems.
LangGraph offers a flexible framework for defining agent graphs with conditional logic, cycles, and state management.

This document is organized as follows:

- **[Quick Start](#quick-start)** - Your first durable LangGraph agent
- **[Per-Node Configuration](#per-node-configuration)** - Configuring timeouts, retries, and task queues
- **[Agentic Execution](#agentic-execution)** - Using LangChain's create_agent with Temporal
- **[Human-in-the-Loop](#human-in-the-loop-interrupts)** - Supporting interrupt() with Temporal signals
- **[Graph Visualization](#graph-visualization-queries)** - ASCII and Mermaid diagrams via queries
- **[Command API](#command-api-dynamic-routing)** - Dynamic routing with Command(goto=...)
- **[Sample Applications](#sample-applications)** - Complete working examples
- **[Functional API](#functional-api-entrypointtask)** - Using @entrypoint/@task decorators

## Architecture

The diagram below shows how LangGraph integrates with Temporal.
Each graph node executes as a Temporal activity, providing automatic retries and failure recovery.
The workflow orchestrates the graph execution, maintaining state and handling interrupts.

```text
            +---------------------+
            |   Temporal Server   |      (Stores workflow state,
            +---------------------+       schedules activities,
                     ^                    persists progress)
                     |
        Save state,  |   Schedule Tasks,
        progress,    |   load state on resume
        timeouts     |
                     |
+------------------------------------------------------+
|                      Worker                          |
|   +----------------------------------------------+   |
|   |              Workflow Code                   |   |
|   |       (LangGraph Orchestration)              |   |
|   +----------------------------------------------+   |
|          |          |                |               |
|          v          v                v               |
|   +-----------+ +-----------+ +-------------+        |
|   | Activity  | | Activity  | |  Activity   |        |
|   | (Node 1)  | | (Node 2)  | | (LLM Call)  |        |
|   +-----------+ +-----------+ +-------------+        |
|         |           |                |               |
+------------------------------------------------------+
          |           |                |
          v           v                v
      [External APIs, LLM providers, databases, etc.]
```

## Installation

> **Note:** This integration is not yet released. Install from the development branch:

```bash
# Install temporalio from the development branch (currently on fork)
pip install "temporalio @ git+https://github.com/mfateev/sdk-python.git@langgraph-plugin"

# Install LangGraph dependencies
pip install langgraph langchain-core
```

## Quick Start

```python
from langgraph.graph import StateGraph, START, END
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, compile
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker
from typing_extensions import TypedDict


# 1. Define your state
class MyState(TypedDict, total=False):
    query: str
    result: str


# 2. Define node functions
def process_query(state: MyState) -> MyState:
    return {"result": f"Processed: {state.get('query', '')}"}


# 3. Create a graph builder function
def build_my_graph():
    graph = StateGraph(MyState)
    graph.add_node("process", process_query)
    graph.add_edge(START, "process")
    graph.add_edge("process", END)
    return graph.compile()


# 4. Define your workflow
@workflow.defn
class MyAgentWorkflow:
    @workflow.run
    async def run(self, query: str) -> dict:
        app = compile("my_graph")
        return await app.ainvoke({"query": query})


# 5. Run with Temporal
async def main():
    # Create plugin with registered graphs
    plugin = LangGraphPlugin(
        graphs={"my_graph": build_my_graph}
    )

    # Connect to Temporal (uses TEMPORAL_* env vars if set)
    config = ClientConfig.load_client_connect_config()
    config.setdefault("target_host", "localhost:7233")
    client = await Client.connect(**config, plugins=[plugin])

    # Start worker
    async with Worker(
        client,
        task_queue="langgraph-queue",
        workflows=[MyAgentWorkflow],
    ):
        # Execute workflow
        result = await client.execute_workflow(
            MyAgentWorkflow.run,
            "Hello, world!",
            id="my-workflow-1",
            task_queue="langgraph-queue",
        )
        print(result)
```

## Plugin-Level Configuration

Set default activity options at the plugin level to avoid repeating configuration in every workflow:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

# Create plugin with default options for all graphs
plugin = LangGraphPlugin(
    graphs={"my_graph": build_my_graph},
    # Default options for all nodes across all graphs
    default_activity_options=activity_options(
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=RetryPolicy(maximum_attempts=5),
    ),
    # Per-node options (applies to all graphs with matching node names)
    per_node_activity_options={
        "llm_call": activity_options(
            start_to_close_timeout=timedelta(minutes=30),
            task_queue="llm-workers",
        ),
    },
)
```

Plugin-level options are merged with `compile()` options, with `compile()` taking precedence. See [Configuration Priority](#configuration-priority) for details.

## Per-Node Configuration

Configure timeouts, retries, and task queues per node using `activity_options()`:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import activity_options

def build_configured_graph():
    graph = StateGraph(MyState)

    # Fast node with short timeout
    graph.add_node(
        "validate",
        validate_input,
        metadata=activity_options(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    )

    # External API with retries
    graph.add_node(
        "fetch_data",
        fetch_from_api,
        metadata=activity_options(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=5,
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
            ),
        ),
    )

    # GPU processing on specialized workers
    graph.add_node(
        "process_gpu",
        gpu_processing,
        metadata=activity_options(
            start_to_close_timeout=timedelta(hours=1),
            task_queue="gpu-workers",
        ),
    )

    # Combining with other metadata
    graph.add_node(
        "custom_node",
        custom_func,
        metadata=activity_options(
            start_to_close_timeout=timedelta(minutes=5),
        ) | {"custom_key": "custom_value"},
    )

    # ... add edges ...
    return graph.compile()
```

### Configuration Options

All parameters mirror `workflow.execute_activity()` options:

| Option | `activity_options()` Parameter | Description |
|--------|--------------------------------------|-------------|
| Start-to-Close Timeout | `start_to_close_timeout` | Max time for a single execution attempt |
| Schedule-to-Close Timeout | `schedule_to_close_timeout` | Total time including retries |
| Schedule-to-Start Timeout | `schedule_to_start_timeout` | Max time waiting to start |
| Heartbeat Timeout | `heartbeat_timeout` | Interval for long-running activities |
| Task Queue | `task_queue` | Route to specialized workers |
| Retry Policy | `retry_policy` | Temporal `RetryPolicy` (overrides LangGraph's) |
| Cancellation Type | `cancellation_type` | How cancellation is handled |
| Versioning Intent | `versioning_intent` | Worker Build ID versioning |
| Summary | `summary` | Human-readable activity description |
| Priority | `priority` | Task queue ordering priority |

You can also use LangGraph's native `retry_policy` parameter on `add_node()`, which is automatically mapped to Temporal's retry policy. If both are specified, `activity_options(retry_policy=...)` takes precedence.

### Running Nodes in the Workflow

By default, all nodes run as Temporal activities. Use `temporal_node_metadata(run_in_workflow=True)` when you need to call Temporal operations directly from a node:

```python
from temporalio import workflow
from temporalio.contrib.langgraph import temporal_node_metadata

async def orchestrator_node(state: dict) -> dict:
    """Node that orchestrates multiple activity calls from the workflow.

    This node runs directly in the workflow (run_in_workflow=True) so it can:
    - Call multiple Temporal activities
    - Use workflow features like timers, signals, queries
    - Implement complex orchestration logic
    """
    data = state.get("data", "")

    # Call validation activity
    is_valid = await workflow.execute_activity(
        "validate_data",
        data,
        start_to_close_timeout=timedelta(seconds=30),
    )

    if not is_valid:
        return {"validated": False, "result": "Validation failed"}

    # Call enrichment activity
    enriched = await workflow.execute_activity(
        "enrich_data",
        data,
        start_to_close_timeout=timedelta(seconds=30),
    )

    return {"validated": True, "enriched_data": enriched}


def finalize_node(state: dict) -> dict:
    """Final processing - runs as a regular activity."""
    enriched = state.get("enriched_data", "")
    return {"result": f"Processed: {enriched}"}


# Build graph with in-workflow nodes
graph = StateGraph(MyState)
graph.add_node(
    "orchestrator",
    orchestrator_node,
    metadata=temporal_node_metadata(run_in_workflow=True),  # Runs in workflow
)
graph.add_node("finalize", finalize_node)  # Runs as activity (default)
graph.add_edge(START, "orchestrator")
graph.add_edge("orchestrator", "finalize")
graph.add_edge("finalize", END)
```

**When to use `run_in_workflow=True`:**
- Calling Temporal activities with custom configuration
- Starting child workflows
- Using workflow timers or sleep
- Any node that needs direct access to Temporal workflow APIs

**When to keep as activity (default):**
- LLM calls and other LangChain operations
- Operations that don't need Temporal workflow primitives
- Code that uses libraries incompatible with the workflow sandbox

> **Note:** Nodes running in the workflow execute within Temporal's workflow sandbox, which enforces determinism. Ensure your in-workflow node functions only use Temporal APIs and deterministic Python code. See the [activity_from_node sample](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/activity_from_node) for a complete example.

## Agentic Execution

LangChain's agent APIs work directly with the Temporal integration. Each graph node (agent reasoning, tool execution) runs as a Temporal activity, providing automatic retries and failure recovery.

### Using create_agent

Use LangChain's `create_agent` with your model and tools:

```python
from datetime import timedelta
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from temporalio import workflow
from temporalio.contrib.langgraph import (
    activity_options,
    LangGraphPlugin,
    compile,
)


@tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"


@tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Weather in {city}: Sunny, 72°F"


def build_agent_graph():
    model = ChatOpenAI(model="gpt-4o")
    return create_agent(model, [search_web, get_weather])


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, query: str) -> dict:
        app = compile("my_agent")
        return await app.ainvoke({"messages": [{"role": "user", "content": query}]})


# Register with plugin
plugin = LangGraphPlugin(graphs={"my_agent": build_agent_graph})
```

### How It Works

When you use `create_agent`, LangGraph creates a graph with two main nodes:
- **agent**: Calls the LLM to decide what to do next
- **tools**: Executes the tools the LLM requested

The Temporal integration runs each node as a separate activity. The agentic loop (agent → tools → agent → tools → ...) continues until the LLM decides to stop. Each activity execution is:
- **Durable**: Progress is saved after each node completes
- **Retryable**: Failed nodes can be automatically retried
- **Recoverable**: If the worker crashes, execution resumes from the last completed node

### Configuring Activity Options

Configure timeouts and retries for agent nodes at the plugin or compile level:

```python
from temporalio.common import RetryPolicy

plugin = LangGraphPlugin(
    graphs={"my_agent": build_agent_graph},
    per_node_activity_options={
        # Agent node makes LLM calls - give it time
        "agent": activity_options(
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        ),
        # Tools node runs tool functions
        "tools": activity_options(
            start_to_close_timeout=timedelta(minutes=2),
        ),
    },
)
```

### Key Benefits

- **No Special Wrappers Needed**: Use native LangGraph APIs directly
- **Durable Execution**: Each node execution is persisted by Temporal
- **Automatic Retries**: Failed LLM calls or tool executions are retried
- **Crash Recovery**: Execution resumes from last completed node after failures

### Subgraph Support

When you add a compiled subgraph as a node in an outer graph, the Temporal integration automatically detects it and executes each **inner node** as a separate activity. This provides finer-grained durability than running the subgraph as a single activity.

```python
from langgraph.graph import StateGraph, START, END

# Example 1: Explicit subgraph
def build_graph_with_subgraph():
    # Create child subgraph
    child = StateGraph(ChildState)
    child.add_node("child_process", child_process_node)
    child.add_edge(START, "child_process")
    child.add_edge("child_process", END)
    child_compiled = child.compile()

    # Create parent graph with child as a node
    parent = StateGraph(ParentState)
    parent.add_node("parent_start", parent_start_node)
    parent.add_node("child_graph", child_compiled)  # Subgraph as a node
    parent.add_node("parent_end", parent_end_node)
    parent.add_edge(START, "parent_start")
    parent.add_edge("parent_start", "child_graph")
    parent.add_edge("child_graph", "parent_end")
    parent.add_edge("parent_end", END)
    return parent.compile()


# Example 2: Agent as subgraph
def build_graph_with_agent_subgraph():
    # Create an agent subgraph using create_agent
    model = ChatOpenAI(model="gpt-4o")
    agent_subgraph = create_agent(model, [search_web, get_weather])

    # Add the agent as a node in an outer graph
    outer = StateGraph(AgentState)
    outer.add_node("my_agent", agent_subgraph)  # Agent subgraph as a node
    outer.add_node("post_process", post_process_fn)
    outer.add_edge(START, "my_agent")
    outer.add_edge("my_agent", "post_process")
    outer.add_edge("post_process", END)
    return outer.compile()
```

When subgraphs execute:
- Each inner node runs as a **separate Temporal activity**
- Each inner node has its own retry/timeout configuration
- If the worker crashes during the subgraph, execution resumes from the last completed inner node
- Nested subgraphs are also recursively flattened

This automatic subgraph detection means you get full durability without manually adding each node. Subgraphs are automatically registered with composite IDs (e.g., `outer_graph:my_agent`) for activity lookup.

## Human-in-the-Loop (Interrupts)

Use LangGraph's `interrupt()` to pause for human input:

```python
from langgraph.types import interrupt, Command


def approval_node(state: dict) -> dict:
    """Node that requests human approval."""
    response = interrupt({
        "question": "Do you approve?",
        "data": state.get("data"),
    })
    return {"approved": response.get("approved", False)}


@workflow.defn
class ApprovalWorkflow:
    def __init__(self):
        self._human_response = None
        self._interrupt_value = None

    @workflow.signal
    def provide_approval(self, response: dict):
        """Signal to provide approval response."""
        self._human_response = response

    @workflow.query
    def get_pending_approval(self) -> dict | None:
        """Query to check if approval is pending and get details."""
        return self._interrupt_value

    @workflow.query
    def get_status(self) -> str:
        """Query to get the current workflow status."""
        if self._interrupt_value is None:
            return "processing"
        elif self._human_response is None:
            return "waiting_for_approval"
        else:
            return "approved" if self._human_response.get("approved") else "rejected"

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        app = compile("approval_graph")
        result = await app.ainvoke(input_data)

        # Check for interrupt
        if "__interrupt__" in result:
            # Store interrupt value for queries
            self._interrupt_value = result["__interrupt__"][0].value

            # Notify approvers (email, Slack, etc.)
            await workflow.execute_activity(
                notify_approver,
                self._interrupt_value,
                start_to_close_timeout=timedelta(seconds=30),
            )

            # Wait for human input via signal (with optional timeout)
            await workflow.wait_condition(
                lambda: self._human_response is not None
            )

            # Resume with human response
            result = await app.ainvoke(Command(resume=self._human_response))

        return result
```

See the [human_in_the_loop samples](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/human_in_the_loop) for complete examples with timeout handling.

### Using Temporal Signals/Updates Directly

As an alternative to LangGraph's `interrupt()`, you can use Temporal signals or updates directly from nodes that run in the workflow context (`run_in_workflow=True`). This gives you full access to Temporal's workflow primitives:

```python
from temporalio import workflow
from temporalio.contrib.langgraph import temporal_node_metadata


@workflow.defn
class DirectSignalWorkflow:
    def __init__(self):
        self._user_input = None

    @workflow.signal
    def provide_input(self, data: dict):
        """Signal to provide user input."""
        self._user_input = data

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        app = compile("my_graph")
        return await app.ainvoke(input_data)


async def wait_for_user_node(state: dict) -> dict:
    """Node that waits for user input via Temporal signal.

    This node runs in the workflow context and can access
    workflow instance state directly.
    """
    # Access the workflow instance
    wf = workflow.instance()

    # Wait for signal
    await workflow.wait_condition(lambda: wf._user_input is not None)

    return {"user_response": wf._user_input}


# Register node to run in workflow
graph = StateGraph(MyState)
graph.add_node(
    "wait_for_user",
    wait_for_user_node,
    metadata=temporal_node_metadata(run_in_workflow=True),
)
```

This approach simplifies long-running human-in-the-loop scenarios by keeping the wait logic inside the graph node rather than handling interrupts at the workflow level.

## Graph Visualization Queries

The `TemporalLangGraphRunner` provides methods to visualize graph structure and execution progress. Expose these via Temporal queries to monitor running workflows:

```python
from dataclasses import dataclass
from typing import Any, cast

from temporalio import workflow
from temporalio.contrib.langgraph import compile

# Import your graph's state type
from my_graph import MyState


@dataclass
class GraphStateResponse:
    """Response from get_graph_state query."""
    values: MyState
    next: list[str]
    step: int
    interrupted: bool
    interrupt_node: str | None
    interrupt_value: dict[str, Any] | None


@workflow.defn
class MyWorkflow:
    def __init__(self):
        self._app = None

    @workflow.query
    def get_graph_ascii(self) -> str:
        """Get ASCII diagram of graph execution progress."""
        if self._app is None:
            return "Graph not yet initialized"
        return self._app.get_graph_ascii()

    @workflow.query
    def get_graph_mermaid(self) -> str:
        """Get Mermaid diagram with nodes colored by status."""
        if self._app is None:
            return "Graph not yet initialized"
        return self._app.get_graph_mermaid()

    @workflow.query
    def get_graph_state(self) -> GraphStateResponse:
        """Get current graph execution state."""
        if self._app is None:
            return GraphStateResponse(
                values=cast(MyState, {}), next=[], step=0,
                interrupted=False, interrupt_node=None, interrupt_value=None,
            )
        snapshot = self._app.get_state()
        interrupt_task = snapshot.tasks[0] if snapshot.tasks else None
        return GraphStateResponse(
            values=cast(MyState, snapshot.values),
            next=list(snapshot.next),
            step=snapshot.metadata.get("step", 0) if snapshot.metadata else 0,
            interrupted=bool(snapshot.tasks),
            interrupt_node=interrupt_task.get("interrupt_node") if interrupt_task else None,
            interrupt_value=interrupt_task.get("interrupt_value") if interrupt_task else None,
        )

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        self._app = compile("my_graph")
        return await self._app.ainvoke(input_data)
```

Query the workflow from the CLI:

```bash
# ASCII diagram with progress indicators
temporal workflow query --workflow-id my-workflow --type get_graph_ascii
# Output:
# ┌───────────────────┐
# │       START       │ ✓
# └─────────┬─────────┘
#           │
#           ▼
# ┌───────────────────┐
# │  request_approval │ ▶ INTERRUPTED
# └─────────┬─────────┘
#           │
#           ▼
# ┌───────────────────┐
# │        END        │ ○
# └───────────────────┘
# Legend: ✓ completed  ▶ current/interrupted  ○ pending

# Mermaid diagram (renders in GitHub, Notion, etc.)
temporal workflow query --workflow-id my-workflow --type get_graph_mermaid

# Full state with typed values
temporal workflow query --workflow-id my-workflow --type get_graph_state
```

The visualization methods show:
- **Completed nodes** (✓ / green): Nodes that have finished executing
- **Current node** (▶ / yellow): Node currently executing or interrupted
- **Pending nodes** (○ / gray): Nodes not yet executed

## Store API (Cross-Node Persistence)

Use LangGraph's Store for data persistence across nodes:

```python
from langgraph.config import get_store


def node_with_store(state: dict) -> dict:
    store = get_store()
    user_id = state.get("user_id")

    # Read from store
    item = store.get(("user", user_id), "preferences")
    prefs = item.value if item else {}

    # Write to store
    store.put(("user", user_id), "preferences", {"theme": "dark"})

    return {"preferences": prefs}
```

Store data persists across nodes within the same workflow execution and can be checkpointed for continue-as-new.

## Command API (Dynamic Routing)

Use LangGraph's `Command` to combine state updates with dynamic navigation:

```python
from langgraph.types import Command

def router_node(state: dict) -> Command:
    """Node that routes based on state."""
    if state.get("value", 0) > 10:
        # Skip to finish node
        return Command(goto="finish", update={"path": ["router"]})
    else:
        # Go through processing
        return Command(goto="process", update={"path": ["router"]})


def process_node(state: dict) -> dict:
    """Regular processing node."""
    path = state.get("path", [])
    return {"value": state["value"] * 2, "path": path + ["process"]}


def finish_node(state: dict) -> dict:
    """Final node."""
    path = state.get("path", [])
    return {"path": path + ["finish"]}


# Build graph - Command handles routing, no static edges needed from router
graph = StateGraph(MyState)
graph.add_node("router", router_node)
graph.add_node("process", process_node)
graph.add_node("finish", finish_node)
graph.add_edge(START, "router")
graph.add_edge("process", "finish")
graph.add_edge("finish", END)
```

> **Note:** When using `Command(goto=...)`, don't add static edges from that node—the Command determines routing. If both exist, both paths will execute.

## Continue-as-New (Long-Running Workflows)

For workflows that may generate large event histories:

```python
@workflow.defn
class LongRunningWorkflow:
    @workflow.run
    async def run(self, input_data: dict, checkpoint: dict | None = None) -> dict:
        # Restore from checkpoint if provided
        app = compile("my_graph", checkpoint=checkpoint)

        # Use should_continue to check if continue-as-new is suggested
        def should_continue():
            return not workflow.info().is_continue_as_new_suggested()

        result = await app.ainvoke(input_data, should_continue=should_continue)

        # If stopped for checkpointing, continue-as-new
        if "__checkpoint__" in result:
            snapshot = result["__checkpoint__"]
            workflow.continue_as_new(input_data, snapshot.model_dump())

        return result
```

## Compile Options

The `compile()` function accepts these parameters:

```python
from temporalio.common import RetryPolicy

app = compile(
    "graph_id",
    # Default configuration for all nodes (overridden by node metadata)
    default_activity_options=activity_options(
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=RetryPolicy(maximum_attempts=3),
        task_queue="agent-workers",
    ),
    # Per-node configuration (for existing graphs without modifying source)
    per_node_activity_options={
        "slow_node": activity_options(
            start_to_close_timeout=timedelta(hours=2),
        ),
        "gpu_node": activity_options(
            task_queue="gpu-workers",
            start_to_close_timeout=timedelta(hours=1),
        ),
    },
    # Restore from checkpoint for continue-as-new
    checkpoint=None,
)
```

The `default_activity_options` parameter accepts the same options as `activity_options()`. The `per_node_activity_options` parameter allows configuring specific nodes without modifying the graph source code.

### Configuration Priority

Activity options can be set at multiple levels with the following priority (highest to lowest):

1. Node metadata from `add_node(metadata=...)`
2. `per_node_activity_options` from `compile()`
3. `per_node_activity_options` from `LangGraphPlugin()`
4. `default_activity_options` from `compile()`
5. `default_activity_options` from `LangGraphPlugin()`
6. Built-in defaults (5 min timeout, 3 retries)

Options at each level are merged, so you can set base defaults at the plugin level and selectively override specific options in `compile()` or node metadata.

## Sample Applications

For complete working examples, see the [langgraph_plugin](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin) directory in the `langgraph_plugin` branch of the samples repository.

### Graph API Samples

| Sample | Description |
|--------|-------------|
| [hello_world](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/hello_world) | Simple starter demonstrating basic plugin setup |
| [react_agent](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/react_agent) | ReAct agent with tool calling |
| [human_in_the_loop](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/human_in_the_loop) | Human-in-the-loop with interrupt/resume (two approaches) |
| [activity_from_node](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/activity_from_node) | Calling Temporal activities from nodes |
| [supervisor](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/supervisor) | Multi-agent supervisor pattern |
| [agentic_rag](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/agentic_rag) | RAG with document grading |
| [plan_and_execute](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/graph_api/plan_and_execute) | Plan-and-execute pattern |

### Functional API Samples

| Sample | Description |
|--------|-------------|
| [hello_world](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/hello_world) | Simple starter with @task and @entrypoint |
| [react_agent](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/react_agent) | ReAct agent using tasks |
| [human_in_the_loop](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/human_in_the_loop) | Human-in-the-loop with interrupt() |
| [continue_as_new](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/continue_as_new) | Task result caching across continue-as-new |
| [supervisor](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/supervisor) | Multi-agent supervisor pattern |
| [agentic_rag](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/agentic_rag) | RAG with document grading |
| [plan_and_execute](https://github.com/temporalio/samples-python/tree/langgraph_plugin/langgraph_plugin/functional_api/plan_and_execute) | Plan-and-execute pattern |

## Functional API (`@entrypoint`/`@task`)

In addition to the Graph API (`StateGraph`), LangGraph also provides a Functional API using `@entrypoint` and `@task` decorators. This integration supports both APIs.

### Basic Usage

```python
from langgraph.func import entrypoint, task

@task
def fetch_data(url: str) -> dict:
    """Tasks execute as Temporal activities."""
    return requests.get(url).json()

@task
def process_data(data: dict) -> str:
    """Each task is automatically retried on failure."""
    return transform(data)

@entrypoint()
async def my_pipeline(url: str) -> dict:
    """Entrypoint orchestrates task execution."""
    data = await fetch_data(url)
    result = await process_data(data)
    return {"result": result}
```

Register and use in a workflow:

```python
from temporalio.contrib.langgraph import LangGraphPlugin, compile

# Register with the plugin (auto-detects Graph vs Functional API)
plugin = LangGraphPlugin(
    graphs={"my_pipeline": my_pipeline},
    default_activity_options=activity_options(
        start_to_close_timeout=timedelta(minutes=5)
    ),
)

@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, url: str) -> dict:
        app = compile("my_pipeline")
        return await app.ainvoke(url)
```

### Continue-as-New with Task Caching

The Functional API supports continue-as-new via task result caching. When a task completes, its result is cached. After continue-as-new, the cache is restored and previously-completed tasks return immediately without re-execution.

```python
@dataclass
class PipelineInput:
    url: str
    checkpoint: dict | None = None
    phase: int = 1

@workflow.defn
class LongRunningPipeline:
    @workflow.run
    async def run(self, input: PipelineInput) -> dict:
        # Restore cache from checkpoint if continuing
        app = compile("my_pipeline", checkpoint=input.checkpoint)

        if input.phase == 1:
            # First phase: run some tasks
            result = await app.ainvoke({"url": input.url, "stop_after": 3})

            # Capture cache state before continue-as-new
            checkpoint = app.get_state()

            workflow.continue_as_new(PipelineInput(
                url=input.url,
                checkpoint=checkpoint,  # Pass cached task results
                phase=2,
            ))

        # Second phase: remaining tasks use cached results
        return await app.ainvoke({"url": input.url})
```

**Key differences from Graph API:**
- Graph API checkpoints full execution state and can resume mid-graph
- Functional API caches task results; the entrypoint re-executes but cached tasks return instantly

### Task Requirements

Tasks must be importable module-level functions:
- ✅ Functions defined at module level
- ❌ Functions in `__main__`
- ❌ Closures or lambdas
- ❌ Functions defined inside other functions

## Important Notes

### Activity Registration

LangGraph node execution activities (`node`, `tool_node`, `resume_node`) are automatically registered by the plugin. Do not manually add them to the worker.

### Streaming

Real-time streaming is not supported. For progress updates, use:
- Temporal queries to check workflow state
- Activity heartbeats for long-running nodes
- An external system (like Redis) to stream updates to frontends
