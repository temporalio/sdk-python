# LangGraph Temporal Integration - Design Document

**Version:** 4.0
**Status:** Complete

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Graph API Integration](#3-graph-api-integration)
4. [Functional API Integration](#4-functional-api-integration)
5. [Unified Plugin](#5-unified-plugin)
6. [Data Models & Serialization](#6-data-models--serialization)
7. [Feature Support](#7-feature-support)
8. [File Structure](#8-file-structure)

---

## 1. Overview

This integration enables LangGraph graphs to run as Temporal workflows, providing durable execution, automatic retries, and enterprise observability for AI agent applications.

### Supported LangGraph APIs

| API Style | Description | Runner Class |
|-----------|-------------|--------------|
| **Graph API** | `StateGraph` with nodes and edges | `TemporalLangGraphRunner` |
| **Functional API** | `@entrypoint` and `@task` decorators | `TemporalFunctionalRunner` |

### Key Benefits

- **Durable Execution** - Workflows survive crashes and restarts
- **Automatic Retries** - Per-node retry policies with exponential backoff
- **Distributed Scale** - Activities execute across worker pools
- **Enterprise Observability** - Full visibility into execution via Temporal UI
- **Human-in-the-Loop** - Native interrupt/resume support via signals

---

## 2. Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────┐
│                    Temporal Workflow                         │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Graph/Entrypoint Initialization (Deterministic)       │ │
│  │  • Lookup from plugin registry                         │ │
│  │  • Compile runner                                      │ │
│  └────────────────────────────────────────────────────────┘ │
│                            │                                 │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Orchestration Layer (Deterministic)                   │ │
│  │  • Graph API: AsyncPregelLoop tick()/after_tick()      │ │
│  │  • Functional API: Direct entrypoint execution         │ │
│  └────────────────────────────────────────────────────────┘ │
│                            │                                 │
│              ┌─────────────┴─────────────┐                   │
│              │                           │                   │
│              ▼                           ▼                   │
│  ┌────────────────────┐   ┌──────────────────────────────┐ │
│  │ Workflow Execution │   │   Activity Execution         │ │
│  │ (run_in_workflow)  │   │   (Default)                  │ │
│  │                    │   │                              │ │
│  │ • Pure transforms  │   │ • LLM calls                  │ │
│  │ • Routing logic    │   │ • Tool execution             │ │
│  └────────────────────┘   │ • API requests               │ │
│                            └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### Core Principle: Nodes as Activities

By default, all graph nodes and `@task` functions execute as Temporal activities:

1. **LLM calls are non-deterministic** - Same prompt can return different responses
2. **Tool execution has side effects** - API calls, database writes
3. **Activities provide durability** - Results persisted, automatic retries
4. **Timeouts are configurable** - Per-node/task timeout control

Exception: `run_in_workflow=True` metadata allows deterministic operations to run directly in the workflow.

---

## 3. Graph API Integration

The Graph API uses LangGraph's `StateGraph` with explicit nodes and edges.

### Execution Model: BSP (Bulk Synchronous Parallel)

LangGraph uses a BSP model where all ready tasks in a step execute concurrently:

```python
async with loop:
    while loop.tick():  # Prepares tasks based on graph topology
        tasks = [t for t in loop.tasks.values() if not t.writes]
        # Execute ALL tasks in parallel - critical for correctness
        await asyncio.gather(*[self._execute_task(t) for t in tasks])
        loop.after_tick()  # Applies writes to channels
```

### Write Capture Pattern

Nodes don't directly mutate channels - they call a callback to record write intents:

```
Activity:
1. Create local writes deque
2. Inject CONFIG_KEY_SEND callback into config
3. Execute node (writes captured via callback)
4. Return writes as serialized data

Workflow:
1. Receive writes from activity
2. Append to task.writes deque
3. Pregel loop applies writes to channels via after_tick()
```

### TemporalLangGraphRunner

```python
class TemporalLangGraphRunner:
    """Executes StateGraph graphs with Temporal activities."""

    def __init__(
        self,
        pregel: Pregel,
        graph_id: str,
        default_activity_options: dict[str, Any] | None = None,
        per_node_activity_options: dict[str, dict[str, Any]] | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        # Validates no step_timeout (non-deterministic)
        # Stores options for activity configuration

    async def ainvoke(
        self,
        input_state: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Creates AsyncPregelLoop
        # Drives execution via tick()/after_tick()
        # Routes nodes to activities or workflow execution
```

---

## 4. Functional API Integration

The Functional API uses `@entrypoint` and `@task` decorators for a more Pythonic approach.

### Why a Separate Runner?

LangGraph's Pregel runner **always overwrites** `CONFIG_KEY_CALL` with its own implementation. To route `@task` calls to Temporal activities, we must:

1. Extract the entrypoint function from the Pregel wrapper
2. Execute it directly (bypassing Pregel's loop)
3. Inject our own `CONFIG_KEY_CALL` callback via context variable

### CONFIG_KEY_CALL Injection

```python
# LangGraph's @task calls this internally:
def call(func, *args, **kwargs):
    config = get_config()  # From context variable
    impl = config[CONF][CONFIG_KEY_CALL]  # Our injected callback
    return impl(func, (args, kwargs), ...)

# Our callback routes to Temporal:
def temporal_call_callback(func, input, ...):
    if _is_in_workflow_context():
        return self._schedule_task_activity(func, input)
    else:
        return self._execute_task_inline(func, input)
```

### Context Keys Injected

| Key | Purpose |
|-----|---------|
| `CONFIG_KEY_CALL` | Routes @task calls to activities |
| `CONFIG_KEY_SCRATCHPAD` | For `interrupt()` support |
| `CONFIG_KEY_RUNTIME` | For `get_store()`/`get_stream_writer()` |
| `CONFIG_KEY_CHECKPOINT_NS` | Namespace for checkpoints |

### Task Requirements

`@task` functions must be module-level importable:
- NOT in `__main__`
- NOT closures or lambdas
- NOT defined inside other functions

This is because workers need to import the function by its `module.qualname` identifier.

### TemporalFunctionalRunner

```python
class TemporalFunctionalRunner:
    """Executes @entrypoint functions with Temporal task routing."""

    def __init__(
        self,
        entrypoint_id: str,
        default_task_timeout: timedelta = timedelta(minutes=5),
        task_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # Stores entrypoint ID for registry lookup
        # Merges task options from registry and compile()

    async def ainvoke(
        self,
        input_state: Any,
        config: dict[str, Any] | None = None,
        on_interrupt: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        # Extracts entrypoint function from Pregel wrapper
        # Builds config with injected context keys
        # Executes function directly
        # Handles interrupts and parent commands
```

---

## 5. Unified Plugin

### Auto-Detection

The plugin automatically detects whether a registration is a Graph API graph or Functional API entrypoint:

```python
def _is_entrypoint(value: Any) -> bool:
    """Distinguish @entrypoint from StateGraph.compile()"""
    # @entrypoint creates:
    # - Plain Pregel (NOT CompiledStateGraph subclass)
    # - Single node named after the function
    # - NO __start__ node

    # StateGraph.compile() creates:
    # - CompiledStateGraph (subclass of Pregel)
    # - Multiple nodes including __start__
```

### LangGraphPlugin

```python
class LangGraphPlugin(SimplePlugin):
    """Unified plugin for both Graph API and Functional API."""

    def __init__(
        self,
        graphs: dict[str, Pregel | Callable[[], Pregel]] | None = None,
        default_activity_options: dict[str, Any] | None = None,
        activity_options: dict[ActivityOptionsKey, dict[str, Any]] | None = None,
    ) -> None:
        # Accepts both compiled graphs and @entrypoint functions
        # Auto-detects and registers appropriately
        # Configures sandbox passthrough for LangGraph modules
        # Auto-registers node and task execution activities
```

### Activity Options

Options can be scoped globally or per-graph/entrypoint:

```python
plugin = LangGraphPlugin(
    graphs={"my_graph": graph, "my_entrypoint": entrypoint},
    default_activity_options=activity_options(
        start_to_close_timeout=timedelta(minutes=5),
    ),
    activity_options={
        # Global: applies to any graph/entrypoint with this node/task
        "call_model": activity_options(
            start_to_close_timeout=timedelta(minutes=2)
        ),
        # Scoped to specific graph
        ("my_graph", "expensive_node"): activity_options(
            start_to_close_timeout=timedelta(minutes=10),
        ),
    },
)
```

### compile() Function

```python
def compile(
    graph_id: str,
    *,
    default_activity_options: dict[str, Any] | None = None,
    activity_options: dict[str, dict[str, Any]] | None = None,
    checkpoint: dict | None = None,
) -> TemporalLangGraphRunner | TemporalFunctionalRunner:
    """Auto-detects and returns appropriate runner."""
    # Checks both registries
    # Returns TemporalLangGraphRunner for graphs
    # Returns TemporalFunctionalRunner for entrypoints
```

---

## 6. Data Models & Serialization

### Message Type Preservation

LangChain messages in `Any`-typed fields lose type information during serialization:

```python
# Before: AIMessage(content="Hello")
# After:  {"content": "Hello", "type": "ai", ...}  # Dict!
```

Solution: `ChannelWrite` model with `value_type` field:

```python
@dataclass
class ChannelWrite:
    channel: str
    value: Any
    value_type: str | None = None  # "message" or "message_list"

    def reconstruct_value(self) -> Any:
        if self.value_type == "message" and isinstance(self.value, dict):
            return _coerce_to_message(self.value)
        return self.value
```

### Activity I/O Models

| Model | Purpose |
|-------|---------|
| `NodeActivityInput` | Input for graph node execution |
| `NodeActivityOutput` | Output with writes, interrupts, sends |
| `TaskActivityInput` | Input for @task execution |
| `ChannelWrite` | Write with type preservation |
| `StoreSnapshot` | Store data passed to activities |
| `StoreWrite` | Store operations returned from activities |
| `SendPacket` | Serialized Send object |
| `CommandOutput` | Serialized Command for parent routing |

---

## 7. Feature Support

### Implemented Features

| Feature | Graph API | Functional API |
|---------|-----------|----------------|
| Basic execution | ✅ | ✅ |
| Per-node/task timeouts | ✅ | ✅ |
| Retry policies | ✅ | ✅ |
| Human-in-the-Loop (interrupt) | ✅ | ✅ |
| Store (cross-node persistence) | ✅ | - |
| Send API (dynamic parallelism) | ✅ | - |
| Command API (goto navigation) | ✅ | - |
| Subgraphs | ✅ | - |
| Continue-as-new checkpointing | ✅ | - |
| run_in_workflow execution | ✅ | - |

### Out of Scope

- **Streaming** - Use LangGraph directly for streaming use cases
- **LangGraph Checkpointer** - Use Temporal's event history instead

---

## 8. File Structure

```
temporalio/contrib/langgraph/
├── __init__.py              # Public API: compile(), activity_options(), LangGraphPlugin
├── _plugin.py               # Unified plugin with auto-detection
├── _runner.py               # Graph API: TemporalLangGraphRunner
├── _functional_runner.py    # Functional API: TemporalFunctionalRunner
├── _activities.py           # Activities for graph node execution
├── _functional_activity.py  # Activity for @task execution
├── _functional_future.py    # Future types for task results
├── _functional_models.py    # Models for functional API
├── _functional_registry.py  # Registry for entrypoints
├── _graph_registry.py       # Registry for graphs
├── _models.py               # Shared data models (ChannelWrite, etc.)
├── _store.py                # ActivityLocalStore implementation
├── _exceptions.py           # Error types and classification
├── _constants.py            # Shared constants
├── README.md                # User documentation
└── CLAUDE.md                # Design guide for Claude
```

---

**End of Document**
