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
- **[Agentic Execution](#agentic-execution)** - Using create_durable_agent() and create_durable_react_agent()
- **[Human-in-the-Loop](#human-in-the-loop-interrupts)** - Supporting interrupt() with Temporal signals
- **[Compatibility](#compatibility)** - Feature support matrix

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

```bash
pip install temporalio langgraph langchain-core
```

## Quick Start

```python
from datetime import timedelta
from langgraph.graph import StateGraph, START, END
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.langgraph import LangGraphPlugin, compile
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

    # Connect to Temporal
    client = await Client.connect("localhost:7233", plugins=[plugin])

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
from temporalio.contrib.langgraph import LangGraphPlugin, node_activity_options

# Create plugin with default options for all graphs
plugin = LangGraphPlugin(
    graphs={"my_graph": build_my_graph},
    # Default options for all nodes across all graphs
    default_activity_options=node_activity_options(
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=RetryPolicy(maximum_attempts=5),
    ),
    # Per-node options (applies to all graphs with matching node names)
    per_node_activity_options={
        "llm_call": node_activity_options(
            start_to_close_timeout=timedelta(minutes=30),
            task_queue="llm-workers",
        ),
    },
)
```

Plugin-level options are merged with `compile()` options, with `compile()` taking precedence. See [Configuration Priority](#configuration-priority) for details.

## Per-Node Configuration

Configure timeouts, retries, and task queues per node using `node_activity_options()`:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import node_activity_options

def build_configured_graph():
    graph = StateGraph(MyState)

    # Fast node with short timeout
    graph.add_node(
        "validate",
        validate_input,
        metadata=node_activity_options(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    )

    # External API with retries
    graph.add_node(
        "fetch_data",
        fetch_from_api,
        metadata=node_activity_options(
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
        metadata=node_activity_options(
            start_to_close_timeout=timedelta(hours=1),
            task_queue="gpu-workers",
        ),
    )

    # Combining with other metadata
    graph.add_node(
        "custom_node",
        custom_func,
        metadata=node_activity_options(
            start_to_close_timeout=timedelta(minutes=5),
        ) | {"custom_key": "custom_value"},
    )

    # ... add edges ...
    return graph.compile()
```

### Configuration Options

All parameters mirror `workflow.execute_activity()` options:

| Option | `node_activity_options()` Parameter | Description |
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

You can also use LangGraph's native `retry_policy` parameter on `add_node()`, which is automatically mapped to Temporal's retry policy. If both are specified, `node_activity_options(retry_policy=...)` takes precedence.

## Agentic Execution

Run LLM-powered agents with durable tool execution and model calls.

### Using Durable Agent Functions (Recommended)

The simplest way to create durable agents is with `create_durable_agent` or `create_durable_react_agent`. These functions automatically wrap the model and tools for Temporal durability:

```python
from datetime import timedelta
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from temporalio import workflow
from temporalio.contrib.langgraph import (
    create_durable_agent,
    LangGraphPlugin,
    compile,
)


@tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"


def build_agent_graph():
    # Just pass your model and tools - wrapping is automatic!
    return create_durable_agent(
        ChatOpenAI(model="gpt-4o"),
        [search_web],
        model_start_to_close_timeout=timedelta(minutes=2),
        tool_start_to_close_timeout=timedelta(minutes=1),
    )


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, query: str) -> dict:
        app = compile("my_agent")
        return await app.ainvoke({"messages": [{"role": "user", "content": query}]})


# Register with plugin
plugin = LangGraphPlugin(graphs={"my_agent": build_agent_graph})
```

For LangGraph's prebuilt agent, use `create_durable_react_agent`:

```python
from temporalio.contrib.langgraph import create_durable_react_agent


def build_react_agent():
    return create_durable_react_agent(
        ChatOpenAI(model="gpt-4o"),
        [search_web],
    )
```

These functions:
- Auto-wrap the model with `temporal_model()` for durable LLM calls
- Auto-wrap tools with `temporal_tool()` for durable tool execution
- Mark agent nodes to run inline in the workflow (model/tool calls as activities)

This provides fine-grained durability where each LLM call and tool invocation is individually retryable and recoverable.

### Manual Wrapping (Advanced)

For more control, you can manually wrap models and tools:

```python
from langchain.agents import create_agent
from temporalio.contrib.langgraph import temporal_model, temporal_tool


def build_agent_graph():
    # Manually wrap model for durable LLM calls
    model = temporal_model(
        ChatOpenAI(model="gpt-4o"),
        start_to_close_timeout=timedelta(minutes=2),
    )

    # Manually wrap tools for durable execution
    tools = [
        temporal_tool(search_web, start_to_close_timeout=timedelta(minutes=1)),
    ]

    return create_agent(model=model, tools=tools)
```

### Hybrid Execution (Advanced)

For deterministic nodes that don't require durability, you can mark them to run directly in the workflow using `temporal_node_metadata()`:

```python
from temporalio.contrib.langgraph import temporal_node_metadata, node_activity_options

# Mark a specific node to run in workflow instead of as an activity
graph.add_node(
    "validate",
    validate_input,
    metadata=temporal_node_metadata(run_in_workflow=True),  # Deterministic, no I/O
)

# Combine with activity options
graph.add_node(
    "process",
    process_data,
    metadata=temporal_node_metadata(
        activity_options=node_activity_options(
            start_to_close_timeout=timedelta(minutes=5),
            task_queue="gpu-workers",
        ),
        run_in_workflow=False,  # Run as activity (default)
    ),
)
```

### Direct Tool Binding

You can also use `bind_tools()` directly on a `temporal_model()` wrapper. This is useful when building custom graphs or using patterns that require explicit tool binding:

```python
from temporalio.contrib.langgraph import temporal_model
from langchain_core.tools import tool


@tool
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Weather in {city}: Sunny, 72°F"


def build_custom_graph():
    # Create temporal model with tools bound
    model = temporal_model(
        "gpt-4o",
        start_to_close_timeout=timedelta(minutes=2),
    )
    model_with_tools = model.bind_tools([get_weather], tool_choice="auto")

    # Use in your custom graph
    graph = StateGraph(MyState)
    graph.add_node("agent", lambda state: {"response": model_with_tools.invoke(state["messages"])})
    # ... add edges ...
    return graph.compile()
```

The bound tools are serialized and passed to the activity, where they are bound to the actual model instance before execution.

### Key Benefits

- **Durable LLM Calls**: Each model invocation is a separate activity with retries
- **Durable Tool Execution**: Tool calls survive failures and can be retried
- **Middleware Support**: `create_agent` supports hooks for human-in-the-loop, summarization, etc.
- **Tool Binding**: Use `bind_tools()` on temporal models for custom graph patterns

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

    @workflow.signal
    def provide_approval(self, response: dict):
        self._human_response = response

    @workflow.run
    async def run(self, input_data: dict) -> dict:
        app = compile("approval_graph")
        result = await app.ainvoke(input_data)

        # Check for interrupt
        if "__interrupt__" in result:
            interrupt_info = result["__interrupt__"][0]
            # interrupt_info.value contains the data passed to interrupt()

            # Request approval from external system (email, Slack, etc.)
            await workflow.execute_activity(
                request_approval,
                interrupt_info.value,
                start_to_close_timeout=timedelta(seconds=30),
            )

            # Wait for human input via signal
            await workflow.wait_condition(
                lambda: self._human_response is not None
            )

            # Resume with human response
            result = await app.ainvoke(Command(resume=self._human_response))

        return result
```

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
    default_activity_options=node_activity_options(
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=RetryPolicy(maximum_attempts=3),
        task_queue="agent-workers",
    ),
    # Per-node configuration (for existing graphs without modifying source)
    per_node_activity_options={
        "slow_node": node_activity_options(
            start_to_close_timeout=timedelta(hours=2),
        ),
        "gpu_node": node_activity_options(
            task_queue="gpu-workers",
            start_to_close_timeout=timedelta(hours=1),
        ),
    },
    # Restore from checkpoint for continue-as-new
    checkpoint=None,
)
```

The `default_activity_options` parameter accepts the same options as `node_activity_options()`. The `per_node_activity_options` parameter allows configuring specific nodes without modifying the graph source code.

### Configuration Priority

Activity options can be set at multiple levels with the following priority (highest to lowest):

1. Node metadata from `add_node(metadata=...)`
2. `per_node_activity_options` from `compile()`
3. `per_node_activity_options` from `LangGraphPlugin()`
4. `default_activity_options` from `compile()`
5. `default_activity_options` from `LangGraphPlugin()`
6. Built-in defaults (5 min timeout, 3 retries)

Options at each level are merged, so you can set base defaults at the plugin level and selectively override specific options in `compile()` or node metadata.

## Full Example

See [`example.py`](./example.py) for a complete customer support agent example demonstrating:

- Multi-node graph with conditional routing
- Per-node timeouts and retry policies
- LangChain message handling
- Integration with Temporal workflows

Run with:

```bash
# Start Temporal server
temporal server start-dev

# Run the example
python -m temporalio.contrib.langgraph.example
```

## Important Notes

### Activity Registration

Activities are automatically registered by the plugin. Do not manually add them to the worker.

### Streaming

Real-time streaming is not supported. For progress updates, use:
- Temporal queries to check workflow state
- Activity heartbeats for long-running nodes

### Subgraphs

Subgraphs execute inline. For better isolation, use child workflows:

```python
@workflow.defn
class SubgraphWorkflow:
    @workflow.run
    async def run(self, input_data: dict) -> dict:
        app = compile("subgraph")
        return await app.ainvoke(input_data)


# In parent graph node
async def node_with_subgraph(state: dict) -> dict:
    result = await workflow.execute_child_workflow(
        SubgraphWorkflow.run,
        state["data"],
        id=f"subgraph-{state['id']}",
    )
    return {"subgraph_result": result}
```

## Compatibility

| Feature | Support |
|---------|---------|
| StateGraph | Full |
| Conditional edges | Full |
| Send API | Full |
| ToolNode | Full |
| create_durable_agent | Full |
| create_durable_react_agent | Full |
| temporal_model / temporal_tool | Full |
| interrupt() | Full |
| Store API | Full |
| Streaming | Limited (via queries) |
