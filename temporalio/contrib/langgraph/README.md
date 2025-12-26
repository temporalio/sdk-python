# Temporal LangGraph Integration

Run LangGraph agents with Temporal for durable execution, automatic retries, and enterprise observability.

## Features

- **Durable Execution**: Graph execution survives process restarts and failures
- **Automatic Retries**: Per-node retry policies with exponential backoff
- **Distributed Scale**: Route different nodes to specialized workers (GPU, high-memory)
- **Human-in-the-Loop**: Support for `interrupt()` with Temporal signals
- **Cross-Node Persistence**: LangGraph Store API for sharing data between nodes
- **Enterprise Observability**: Full visibility via Temporal UI and metrics

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
from temporalio.worker import Worker, UnsandboxedWorkflowRunner
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
        workflow_runner=UnsandboxedWorkflowRunner(),
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

## Per-Node Configuration

Configure timeouts, retries, and task queues per node using `temporal_node_metadata()`:

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import temporal_node_metadata

def build_configured_graph():
    graph = StateGraph(MyState)

    # Fast node with short timeout
    graph.add_node(
        "validate",
        validate_input,
        metadata=temporal_node_metadata(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    )

    # External API with retries
    graph.add_node(
        "fetch_data",
        fetch_from_api,
        metadata=temporal_node_metadata(
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
        metadata=temporal_node_metadata(
            start_to_close_timeout=timedelta(hours=1),
            task_queue="gpu-workers",
        ),
    )

    # Combining with other metadata
    graph.add_node(
        "custom_node",
        custom_func,
        metadata=temporal_node_metadata(
            start_to_close_timeout=timedelta(minutes=5),
        ) | {"custom_key": "custom_value"},
    )

    # ... add edges ...
    return graph.compile()
```

### Configuration Options

All parameters mirror `workflow.execute_activity()` options:

| Option | `temporal_node_metadata()` Parameter | Description |
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
| Workflow Execution | `run_in_workflow` | Run in workflow instead of activity |

You can also use LangGraph's native `retry_policy` parameter on `add_node()`, which is automatically mapped to Temporal's retry policy. If both are specified, `temporal_node_metadata(retry_policy=...)` takes precedence.

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
    defaults=temporal_node_metadata(
        start_to_close_timeout=timedelta(minutes=5),
        retry_policy=RetryPolicy(maximum_attempts=3),
        task_queue="agent-workers",
    ),
    # Enable hybrid execution for deterministic nodes
    enable_workflow_execution=False,
    # Restore from checkpoint for continue-as-new
    checkpoint=None,
)
```

The `defaults` parameter accepts the same options as `temporal_node_metadata()`. Node-specific metadata overrides these defaults.

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

### Workflow Sandbox

LangGraph and LangChain imports contain non-deterministic code. Use `UnsandboxedWorkflowRunner`:

```python
Worker(
    client,
    task_queue="my-queue",
    workflows=[MyWorkflow],
    workflow_runner=UnsandboxedWorkflowRunner(),
)
```

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
| create_react_agent | Full |
| interrupt() | Full |
| Store API | Full |
| Streaming | Limited (via queries) |
