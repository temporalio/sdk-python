# **LangGraph Temporal Integration - Revised Implementation Proposal**

**Version:** 3.1
**Date:** 2025-01-24
**Status:** Final Design - Implementation in Progress

---

## **Table of Contents**

1. [Executive Summary](#executive-summary)
2. [Key Changes from V1](#key-changes-from-v1)
3. [Architecture Overview](#architecture-overview)
4. [Design Decisions](#design-decisions)
5. [Implementation Specification](#implementation-specification)
6. [Usage Examples](#usage-examples)
7. [Testing Strategy](#testing-strategy)
8. [Migration and Compatibility](#migration-and-compatibility)
9. [Performance Considerations](#performance-considerations)
10. [Future Enhancements](#future-enhancements)
11. [References](#references)
12. [Appendix A: Data Types and Serialization](#appendix-a-data-types-and-serialization)
13. [Appendix B: Implementation Checklist](#appendix-b-implementation-checklist)

---

## **1. Executive Summary**

This proposal enables LangGraph graphs to run as Temporal workflows, providing durable execution, automatic retries, distributed scale, and enterprise observability for AI agent applications.

### **Core Principle**

**Plugin-Based Integration:** Register graphs with `LangGraphPlugin`, pass `graph_id` as workflow parameter, and use a clean `compile(graph_id)` API. Following the proven OpenAI Agents plugin pattern.

### **Key Benefits**

- ✅ **Clean Plugin API** - Similar to `OpenAIAgentsPlugin`, activities auto-registered
- ✅ **No Graph Serialization** - Graphs built from registered builders, cached per worker
- ✅ **Simple Integration** - `graph_id` as workflow parameter, `compile(graph_id)` in workflow
- ✅ **Thread-Safe Caching** - Graphs loaded once per worker process, shared across invocations
- ✅ **Minimal Changes** - ~500 LOC, no LangGraph core modifications
- ✅ **Hybrid Execution** - Optional optimization for pure computation nodes
- ✅ **Production Ready** - Built-in retry, timeout, and error handling

---

## **2. Key Changes from V2 (Phase 1 Validation Findings)**

### **Plugin-Based Architecture (V3.1)**

| Aspect | V3.0 Approach | V3.1 Approach | Rationale |
|--------|---------------|---------------|-----------|
| **Graph Registration** | `graph_builder_path` in compile() | `LangGraphPlugin(graphs={...})` | Cleaner API, no module paths in workflow code |
| **Workflow Parameter** | `graph_builder_path` string | `graph_id` string | Simpler, decouples workflow from deployment |
| **Activity Registration** | Manual `activities=[...]` | Auto-registered via plugin | Like `OpenAIAgentsPlugin` |
| **Graph Caching** | Rebuilt per activity call | Cached per worker process | Thread-safe, efficient |
| **Lambda Support** | ❌ Required named functions | ✅ Lambdas work | Cached graph preserves lambda references |

### **Critical Serialization Discovery**

| Aspect | V2 Approach | V3 Approach | Rationale |
|--------|-------------|-------------|-----------|
| **Activity Arguments** | Multiple parameters | Single `NodeActivityInput` Pydantic model | Cleaner interface, better type safety |
| **Activity Return** | `list[tuple[str, Any]]` | `NodeActivityOutput` with `list[ChannelWrite]` | **CRITICAL:** `Any` typed fields lose Pydantic model type info |
| **LangChain Messages** | Assumed auto-serialization | `ChannelWrite` with `value_type` field | Messages in `Any` fields become dicts, require explicit reconstruction |

### **Validation Status**

All 68 prototype tests pass, confirming:

✅ **Loop Execution Model** - `tick()`/`after_tick()` pattern works for driving graph execution (note: `submit` is NOT used for node execution - see section 4.4)
✅ **Write Capture** - `task.writes` deque captures node outputs as `(channel, value)` tuples
✅ **Task Interface** - `PregelExecutableTask` is a frozen dataclass with well-defined fields
✅ **Serialization** - Pydantic converter works with explicit `ChannelWrite` pattern
✅ **Graph Reconstruction** - Activities get cached graphs from plugin registry
✅ **Thread-Safety** - Compiled graphs are immutable, channels created per-invocation (cacheable per worker)

### **Critical Discovery: Message Type Preservation**

**Problem:** LangChain messages in `Any` typed fields lose type information during Temporal serialization:
```python
# Before serialization
writes = [("messages", AIMessage(content="Hello"))]

# After round-trip through Temporal
writes = [("messages", {"content": "Hello", "type": "ai", ...})]  # Dict, not AIMessage!
```

**Solution:** `ChannelWrite` model with `value_type` field enables reconstruction:
```python
class ChannelWrite(BaseModel):
    channel: str
    value: Any
    value_type: str | None = None  # "message" or "message_list"

    def reconstruct_value(self) -> Any:
        if self.value_type == "message" and isinstance(self.value, dict):
            return reconstruct_message(self.value)  # Uses "type" field
        return self.value
```

---

## **2.1 Key Changes from V1 (For Reference)**

### **Architecture Changes from V1**

| Aspect | V1 Approach | V2 Approach | Rationale |
|--------|-------------|-------------|-----------|
| **Graph Definition** | Serialize and pass to workflow | Initialize inside workflow | Eliminates serialization complexity |
| **Node Functions** | Pass as serialized runnables | Use importable functions or registry | Simpler, matches OpenAI pattern |
| **Writers** | Pass separately | Already in `task.proc` | Simplified - writers execute automatically |
| **Subgraphs** | Unclear handling | V1: Skip, V2: Child workflows | Clear migration path |
| **Config** | Full RunnableConfig | Filtered dict | Only serializable parts |
| **Activity Return** | Node output | List of writes `[(channel, value)]` | Captures actual state updates |

### **Issues Resolved in V2**

✅ **No `to_dict()`/`from_dict()` needed** - These methods don't exist
✅ **AsyncPregelLoop initialization** - All required parameters identified
✅ **State write mechanism** - Capture via `CONFIG_KEY_SEND` callback
✅ **Async node support** - Both sync and async nodes handled
✅ **Activity ID uniqueness** - Include step number and execution ID
✅ **Config serialization** - Filter non-serializable objects

---

## **3. Architecture Overview**

### **3.1 High-Level Design**

```
┌─────────────────────────────────────────────────────────────┐
│                    Temporal Workflow                         │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Graph Initialization (Deterministic)                  │ │
│  │  • Build StateGraph                                    │ │
│  │  • Add nodes, edges                                    │ │
│  │  • Compile to Pregel                                   │ │
│  └────────────────────────────────────────────────────────┘ │
│                            │                                 │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  AsyncPregelLoop (Deterministic Orchestration)        │ │
│  │  • tick() - Prepare next tasks                        │ │
│  │  • Evaluate conditional edges                         │ │
│  │  • Manage state/channels                              │ │
│  │  • after_tick() - Apply writes, advance state         │ │
│  └────────────────────────────────────────────────────────┘ │
│                            │                                 │
│                   Custom Task Execution                      │
│                            │                                 │
│              ┌─────────────┴─────────────┐                   │
│              │                           │                   │
│              ▼                           ▼                   │
│  ┌────────────────────┐   ┌──────────────────────────────┐ │
│  │ Workflow Execution │   │   Activity Execution         │ │
│  │ (Deterministic)    │   │   (Non-Deterministic)        │ │
│  │                    │   │                              │ │
│  │ • Pure transforms  │   │ • LLM calls                  │ │
│  │ • Routing logic    │   │ • Tool execution             │ │
│  │ • Child workflows  │   │ • API requests               │ │
│  └────────────────────┘   │ • Database queries           │ │
│                            └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### **3.2 Execution Flow**

```python
# 1. Define graph builder at module level (myapp/agents.py)
def build_weather_agent():
    """Build the weather agent graph - this is importable by module path"""
    graph = StateGraph(AgentState)
    graph.add_node("fetch", fetch_data)      # Activity
    graph.add_node("process", process_data)  # Could be workflow
    graph.add_node("tools", ToolNode(tools)) # Activity
    graph.add_edge("fetch", "process")
    # ... more setup
    return graph.compile()

# 2. Create plugin with registered graphs
plugin = LangGraphPlugin(
    graphs={
        "weather_agent": build_weather_agent,  # graph_id -> builder function
    },
)

# 3. Use plugin with client
client = await Client.connect("localhost:7233", plugins=[plugin])

# 4. Create workflow - graph_id is a parameter
@workflow.defn
class WeatherAgentWorkflow:
    @workflow.run
    async def run(self, graph_id: str, user_input: str):
        # 5. Get compiled runner using graph_id
        app = compile(graph_id)  # Looks up from plugin's registry

        # 6. Execute - orchestration in workflow, I/O as activities
        result = await app.ainvoke({"messages": [("user", user_input)]})
        return result

# 7. Worker inherits plugin config (activities auto-registered)
worker = Worker(client, task_queue="langgraph-workers", workflows=[WeatherAgentWorkflow])

# 8. Execute workflow with graph_id
await client.execute_workflow(
    WeatherAgentWorkflow.run,
    args=["weather_agent", "What's the weather?"],  # graph_id, input
    id="weather-1",
    task_queue="langgraph-workers",
)

# Behind the scenes:
# - Pregel loop runs deterministically in workflow
# - Nodes execute as activities with captured writes
# - Graph is cached per worker process (thread-safe)
# - State updates flow back to workflow via activity results
```

### **3.3 Component Responsibilities**

| Component | Responsibility | Location | Deterministic? |
|-----------|---------------|----------|----------------|
| **Graph Builder** | Define graph structure | Workflow | ✅ Yes |
| **TemporalLangGraphRunner** | Coordinate execution, drive loop | Workflow | ✅ Yes |
| **AsyncPregelLoop** | State/channel management via tick()/after_tick() | Workflow | ✅ Yes |
| **Task Execution** | Route tasks to workflow/activity | Workflow | ✅ Yes |
| **Node Activity** | Execute I/O operations | Activity Worker | ❌ No |
| **Pure Node** | Execute computations | Workflow (optional) | ✅ Yes |

---

## **4. Design Decisions**

### **4.1 Graph Initialization (CRITICAL CHANGE)**

**Decision:** Initialize graph **inside the workflow**, not via serialization.

**V1 Approach:**
```python
# V1: Serialize and pass (COMPLEX, DOESN'T WORK)
graph_dict = graph.to_dict()  # to_dict() doesn't exist!
await client.start_workflow(MyWorkflow, args=[graph_dict, input])
```

**V2 Approach:**
```python
# V2: Initialize in workflow (SIMPLE, WORKS)
@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: dict):
        graph = build_my_graph()  # Define here!
        runner = TemporalLangGraphRunner(graph.compile())
        return await runner.ainvoke(input)
```

**Rationale:**
- ✅ Matches OpenAI Agents pattern (proven approach)
- ✅ No serialization/deserialization complexity
- ✅ Node functions must be importable (good practice)
- ✅ Clear separation: workflow defines logic, activities execute I/O
- ✅ Easy to version and update graph definitions

### **4.2 Node Execution Strategy**

**Decision:** Hybrid execution with **configurable routing**.

**Default:** All nodes as activities (safe, simple)
```python
runner = TemporalLangGraphRunner(graph)  # All nodes → activities
```

**Optimized:** Mark deterministic nodes for workflow execution
```python
@workflow_safe  # Decorator marks pure functions
def transform(state: dict) -> dict:
    return {"result": state["value"] * 2}

runner = TemporalLangGraphRunner(graph)
```

**Routing Logic:**
```python
async def _execute_task(task):
    if is_deterministic(task):
        # Execute directly in workflow (pure computation)
        return await task.proc.ainvoke(task.input, task.config)
    else:
        # Execute as activity (I/O operations)
        return await self._execute_as_activity(task)
```

### **4.3 State Write Capture (CRITICAL)**

**Decision:** Activities capture writes via `CONFIG_KEY_SEND` callback and return them.

#### **The Challenge**

When a node executes in an activity:
1. Activity reconstructs the graph: `graph = build_graph()` (LOCAL instance)
2. Activity gets the node: `node = graph.nodes[node_name].node` (LOCAL reference)
3. Activity executes: `await node.ainvoke(input_data, config)`
4. Writers write to... where exactly? The activity's local channels?

**Question:** How do writes in the activity's local graph propagate back to the workflow's channels?

#### **The Solution: Write Capture Pattern**

The key insight is that **writers don't directly mutate channels** - they call a **callback function** to record the *intent* to write.

**How Writers Work in LangGraph:**

```python
# From LangGraph's _write.py - ChannelWrite.do_write()
def do_write(config: RunnableConfig, writes: Sequence[...]):
    # Get the write callback from config
    write: TYPE_SEND = config[CONF][CONFIG_KEY_SEND]

    # Call it with the writes (doesn't mutate channels directly!)
    write(_assemble_writes(writes))
```

**Key Point:** `CONFIG_KEY_SEND` is a **callback function**, not a channel reference!

#### **Normal LangGraph Execution**

```python
# In _algo.py - when creating a task
configurable={
    CONFIG_KEY_SEND: writes.extend,  # writes is task.writes deque
}

# When writer executes:
# 1. Writer calls ChannelWrite.do_write()
# 2. do_write() calls config[CONFIG_KEY_SEND]
# 3. This appends to task.writes deque
# 4. Pregel loop later reads task.writes and updates channels
```

**The writes are captured in `task.writes`, NOT written to channels immediately!**

#### **Temporal Implementation**

**In Activity:**
```python
@activity.defn
async def execute_langgraph_node(
    node_name: str,
    input_data: Any,
    config_dict: dict,
    step: int,
) -> list[tuple[str, Any]]:
    """
    Execute node and capture write intents.

    IMPORTANT: This does NOT update channels directly!
    Writers call CONFIG_KEY_SEND callback to record writes.
    We capture these as data and return to workflow.
    """

    # Reconstruct graph (activity has its own instance)
    graph = build_graph()
    node = graph.nodes[node_name].node

    # Create LOCAL write capture (NOT shared with workflow!)
    writes: deque[tuple[str, Any]] = deque()

    # Inject capture callback
    # When writers execute, they'll call this function
    config = {
        **config_dict,
        "configurable": {
            **config_dict.get("configurable", {}),
            CONFIG_KEY_SEND: writes.extend,  # Callback appends here
        }
    }

    # Execute node (bound function + writers)
    # Writers will invoke CONFIG_KEY_SEND callback
    # This appends (channel_name, value) tuples to local writes deque
    await node.ainvoke(input_data, config)

    # Return writes as DATA (not channel mutations!)
    # Format: [("channel_name", value), ...]
    return list(writes)
```

**In Workflow:**
```python
async def _execute_as_activity(self, task: PregelExecutableTask):
    """Execute node as activity and apply writes"""

    # Execute activity - returns list of write intents
    writes = await workflow.execute_activity(...)
    # writes = [("messages", msg), ("count", 5)]

    # Apply writes to workflow's task
    # This just appends to task.writes deque in workflow memory
    task.writes.extend(writes)

    # Pregel loop (running in workflow) will process task.writes
    # and update the workflow's channel instances
    # This happens AFTER the activity returns
```

#### **Complete Data Flow**

```
┌─────────────────────────────────────────────────────────────┐
│                        WORKFLOW                              │
│                                                              │
│  1. Pregel loop creates task with empty writes deque        │
│     task.writes = deque()                                   │
│                                                              │
│  2. Send to activity ──────────────────────┐                │
│                                             │                │
└─────────────────────────────────────────────┼────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        ACTIVITY                              │
│                                                              │
│  3. Create local write capture                              │
│     local_writes = deque()                                  │
│                                                              │
│  4. Inject callback into config                             │
│     config[CONFIG_KEY_SEND] = local_writes.extend           │
│                                                              │
│  5. Execute node                                            │
│     await node.ainvoke(input, config)                       │
│       │                                                      │
│       └─> Writers execute                                   │
│           ChannelWrite.do_write(config, [...])              │
│             │                                                │
│             └─> Calls config[CONFIG_KEY_SEND]               │
│                 (which is local_writes.extend)              │
│                   │                                          │
│                   └─> Appends to local_writes               │
│                       [(channel, value), ...]               │
│                                                              │
│  6. Return captured writes                                  │
│     return list(local_writes) ────────────────┐             │
│                                                │             │
└────────────────────────────────────────────────┼─────────────┘
                                                 │
                                                 ▼
┌─────────────────────────────────────────────────────────────┐
│                        WORKFLOW                              │
│                                                              │
│  7. Receive writes from activity                            │
│     writes = [("messages", new_msg), ("output", result)]    │
│                                                              │
│  8. Apply to task                                           │
│     task.writes.extend(writes)                              │
│                                                              │
│  9. Pregel loop processes task.writes                       │
│     for channel, value in task.writes:                      │
│         channels[channel].update(value)  ← Updates workflow │
│                                             channels!        │
└─────────────────────────────────────────────────────────────┘
```

#### **Concrete Example**

```python
# Initial state: {"messages": [], "count": 0}

# Node executes and wants to write: {"messages": [new_msg], "count": 5}

# Writer does:
ChannelWrite.do_write(config, [
    ("messages", [new_msg]),
    ("count", 5)
])

# This calls: config[CONFIG_KEY_SEND]([("messages", [new_msg]), ("count", 5)])

# In activity: appends to local_writes deque
# Activity returns: [("messages", [new_msg]), ("count", 5)]

# In workflow: task.writes.extend([("messages", [new_msg]), ("count", 5)])

# Pregel loop:
channels["messages"].update([new_msg])  # Workflow's channels!
channels["count"].update(5)             # Workflow's channels!
```

#### **Why This Works**

1. **Writers don't mutate state directly** - They call a callback to *record* writes
2. **Activity captures writes as data** - List of (channel_name, value) tuples
3. **Workflow applies writes** - Updates its own channel instances
4. **Channels are NOT shared** - Activity and workflow have separate graph instances, but that's OK because writes are just data!

**Key Insight:** Writers don't write to channels - they write *instructions* to write to channels. These instructions are captured, serialized, returned to the workflow, and applied there.

### **4.4 Loop Execution Model (CRITICAL)**

**Decision:** Drive `AsyncPregelLoop` manually using `tick()`/`after_tick()` instead of intercepting node execution via `submit`.

#### **Why Not Intercept via Submit?**

An earlier approach proposed replacing `loop.submit` to intercept node execution:
```python
# This approach DOES NOT WORK as intended
loop.submit = custom_submit  # submit is not used for node execution!
async for chunk in loop:     # AsyncPregelLoop doesn't support async iteration!
    yield chunk
```

**This doesn't work because:**

1. **`AsyncPregelLoop` doesn't implement async iteration** - There's no `__aiter__`/`__anext__` protocol on the loop itself.

2. **`submit` is NOT used for node execution** - The `submit` function (`AsyncBackgroundExecutor.submit()`) is only used for:
   - Checkpoint saving
   - Write persistence
   - Cache updates
   - Other background I/O

3. **Node execution happens through `PregelRunner.atick()`** which directly calls:
   ```
   PregelRunner.atick() → arun_with_retry() → task.proc.invoke()
   ```
   This bypasses `submit` entirely.

#### **The Correct Approach: Manual Loop Driving**

We use `AsyncPregelLoop` for what it does best (state/channel management) and handle task execution ourselves:

```python
async with loop:
    while loop.tick():  # Prepares tasks based on graph topology
        tasks_to_execute = [t for t in loop.tasks.values() if not t.writes]
        # Execute all tasks in parallel (BSP allows parallelism within tick)
        await asyncio.gather(*[
            self._execute_task(task) for task in tasks_to_execute
        ])
        loop.after_tick()  # Applies writes to channels
```

This follows LangGraph's **BSP (Bulk Synchronous Parallel) model**:

1. **`tick()`** - Analyzes graph topology, prepares tasks for the current step
2. **Execute tasks** - Where we route to Temporal activities
3. **`after_tick()`** - Applies writes to channels, advances to next step

#### **Why This Is Better**

| Aspect | Submit Interception | Manual Loop Driving |
|--------|---------------------|---------------------|
| **API Stability** | Depends on internal `PregelRunner` | Uses public `tick()`/`after_tick()` |
| **Clarity** | Indirect hook, easy to misunderstand | Explicit flow, easy to follow |
| **Control** | Limited to what submit exposes | Full control over execution |
| **Compatibility** | May break with LangGraph updates | Stable BSP model contract |

This approach follows the same pattern as LangGraph's own `main.py`, just with Temporal activities instead of `PregelRunner.atick()`.

---

### **4.5 Prebuilt Node Handling**

**Decision:** Support prebuilt nodes with clear execution model.

| Prebuilt Component | Deterministic? | Execution |
|-------------------|----------------|-----------|
| `ToolNode` | ❌ No | Activity (executes tools with I/O) |
| `tools_condition` | ✅ Yes | Workflow (routing logic) |
| `create_agent` (LangChain 1.0+) | Mixed | Hybrid (orchestration in workflow, tools as activities) |
| `create_react_agent` (legacy) | Mixed | Hybrid (orchestration in workflow, tools as activities) |
| `ValidationNode` | ✅ Yes | Workflow (pure validation) |

**Example with create_agent (LangChain 1.0+, Recommended):**
```python
from langchain.agents import create_agent

@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, user_input: str):
        # Initialize agent using LangChain 1.0+ API
        agent = create_agent(
            model="openai:gpt-4",
            tools=[search_tool, calculator_tool]
        )

        # Wrap with Temporal runner
        runner = TemporalLangGraphRunner(agent)

        # Execute - tools run as activities automatically
        return await runner.ainvoke({
            "messages": [("user", user_input)]
        })
```

**Example with create_react_agent (LangGraph Prebuilt, Legacy):**
```python
from langgraph.prebuilt import create_react_agent, ToolNode

@workflow.defn
class ReactAgentWorkflow:
    @workflow.run
    async def run(self, user_input: str):
        # Initialize prebuilt agent (legacy API)
        agent = create_react_agent(
            ChatOpenAI(model="gpt-4"),
            tools=[search_tool, calculator_tool]
        )

        # Wrap with Temporal runner
        runner = TemporalLangGraphRunner(agent)

        # Execute - ToolNode runs as activity automatically
        return await runner.ainvoke({
            "messages": [("user", user_input)]
        })
```

### **4.5 Configuration Handling**

**Decision:** Filter `RunnableConfig` to only serializable components.

**Problem:** `RunnableConfig` may contain:
- ❌ Callbacks (functions)
- ❌ Run managers (objects)
- ❌ Context managers
- ✅ Tags, metadata (strings/dicts)
- ✅ Recursion limits (ints)

**Solution:**
```python
def _filter_config(config: RunnableConfig) -> dict:
    """Extract only serializable parts of config"""
    return {
        "tags": config.get("tags", []),
        "metadata": config.get("metadata", {}),
        "recursion_limit": config.get("recursion_limit"),
        "max_concurrency": config.get("max_concurrency"),
        # Skip: callbacks, run_name, run_id (non-serializable)
    }
```

### **4.6 Timeout and Retry**

**Decision:** Use Temporal's activity-level timeouts and retries.

**V1:** Tried to use LangGraph's `step_timeout` → Non-deterministic!
**V2:** Disable `step_timeout`, use activity timeouts:

```python
result = await workflow.execute_activity(
    execute_node_activity,
    args=[node_name, input_data, config, step],
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=RetryPolicy(
        maximum_attempts=3,
        backoff_coefficient=2.0,
    ),
    activity_id=f"{node_name}_{step}_{workflow.info().workflow_id}",
)
```

---

## **5. Implementation Specification**

### **5.1 File Structure**

```
temporalio/contrib/langgraph/
├── __init__.py           # Public API: LangGraphPlugin, compile
├── _plugin.py            # LangGraphPlugin implementation
├── runner.py             # TemporalLangGraphRunner
├── activities.py         # Node execution activities
├── models.py             # Pydantic models (ChannelWrite, NodeActivityInput, etc.)
├── _graph_registry.py    # Graph builder registry (internal)
└── testing.py            # Test utilities
```

### **5.2 Core Implementation**

#### **5.2.1 TemporalLangGraphRunner**

**File:** `temporalio/contrib/langgraph/runner.py`

```python
"""Temporal-compatible LangGraph runner"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Optional

try:
    import temporalio.workflow as workflow
    from temporalio.common import RetryPolicy as TemporalRetryPolicy
    TEMPORAL_AVAILABLE = True
except ImportError:
    TEMPORAL_AVAILABLE = False

from langgraph.pregel import Pregel
from langgraph.pregel._loop import AsyncPregelLoop
from langgraph.types import PregelExecutableTask, RetryPolicy


class TemporalLangGraphRunner:
    """
    Temporal-compatible LangGraph execution wrapper.

    Provides the same interface as compiled LangGraph graphs but executes
    node operations as Temporal activities for durable, distributed execution.

    Example:
        @workflow.defn
        class MyWorkflow:
            @workflow.run
            async def run(self, input: dict):
                # Initialize graph in workflow
                graph = StateGraph(MyState)
                graph.add_node("fetch", fetch_data)
                graph.add_node("process", process_data)
                graph.add_edge("fetch", "process")

                # Wrap with Temporal runner
                runner = TemporalLangGraphRunner(graph.compile())

                return await runner.ainvoke(input)

    Architecture:
        - Graph initialization happens in workflow (deterministic)
        - AsyncPregelLoop manages state/channels via tick()/after_tick()
        - Node execution is routed to activities (I/O is non-deterministic)
        - Runner drives the loop manually (not via submit interception)

    Note on Loop Execution:
        We drive AsyncPregelLoop manually using tick()/after_tick() rather than
        trying to intercept LangGraph's internal execution via submit. This is because:
        1. AsyncPregelLoop doesn't support direct async iteration
        2. LangGraph's submit function is for background I/O (checkpoints), not node execution
        3. Node execution happens through PregelRunner.atick() -> arun_with_retry() -> task.proc.invoke()
        4. The tick()/after_tick() pattern follows LangGraph's BSP (Bulk Synchronous Parallel) model
    """

    def __init__(
        self,
        pregel: Pregel,
        graph_id: str,  # V3.1: Graph ID from plugin registry
        default_activity_timeout: Optional[timedelta] = None,
        default_max_retries: int = 3,
        default_task_queue: Optional[str] = None,
    ):
        """
        Initialize Temporal runner.

        Note: Prefer using the compile() function instead of instantiating directly.

        Args:
            pregel: Compiled Pregel instance (from graph.compile())
            graph_id: Graph ID used to look up the builder in the plugin registry.
                Activities use this to get the cached graph.
            default_activity_timeout: Default timeout for node activities.
                Can be overridden per-node via metadata. Default: 5 minutes
            default_max_retries: Default maximum retry attempts.
                Can be overridden per-node via retry_policy. Default: 3
            default_task_queue: Default task queue for activities.
                Can be overridden per-node via metadata. Default: None

        Raises:
            ImportError: If temporalio is not installed
            ValueError: If pregel has step_timeout set (non-deterministic)
        """
        if not TEMPORAL_AVAILABLE:
            raise ImportError(
                "Temporal SDK not installed. "
                "Install with: pip install temporalio"
            )

        # Validate step_timeout is disabled
        if pregel.step_timeout is not None:
            raise ValueError(
                "step_timeout must be None for Temporal execution. "
                "LangGraph's step_timeout uses time.monotonic() which is "
                "non-deterministic. Use per-node metadata instead."
            )

        self.pregel = pregel
        self.graph_id = graph_id  # V3.1: Store for activity input
        self.default_activity_timeout = default_activity_timeout or timedelta(minutes=5)
        self.default_max_retries = default_max_retries
        self.default_task_queue = default_task_queue
        self._step_counter = 0

    async def ainvoke(
        self,
        input: Any,
        config: Optional[dict] = None,
        **kwargs
    ) -> Any:
        """
        Execute graph asynchronously.

        Uses AsyncPregelLoop for state/channel management while routing
        task execution to Temporal activities.

        Args:
            input: Initial input to the graph
            config: Optional configuration dictionary
            **kwargs: Additional arguments (interrupt_before, etc.)

        Returns:
            Final graph state
        """
        # Initialize config
        config = config or {}

        # Create Pregel loop for state management
        loop = AsyncPregelLoop(
            input=input,
            stream=None,
            config=config,
            store=self.pregel.store,
            cache=self.pregel.cache,
            checkpointer=None,  # Use Temporal's event history
            nodes=self.pregel.nodes,
            specs=self.pregel.channels,
            trigger_to_nodes=self.pregel.trigger_to_nodes,
            durability="sync",  # Temporal handles durability
            input_keys=self.pregel.input_channels or [],
            output_keys=self.pregel.output_channels or [],
            stream_keys=self.pregel.stream_channels or [],
            **kwargs
        )

        # Drive the loop manually following BSP model
        async with loop:
            # tick() prepares tasks based on graph topology
            while loop.tick():
                # Execute tasks that don't have writes yet
                tasks_to_execute = [
                    task for task in loop.tasks.values() if not task.writes
                ]

                # Execute all tasks in parallel (BSP allows parallelism within tick,
                # we just need to wait for all before after_tick)
                await asyncio.gather(*[
                    self._execute_task(task) for task in tasks_to_execute
                ])

                # after_tick() applies writes to channels and advances state
                loop.after_tick()

        # Return final output (set by loop.__aexit__)
        return loop.output

    def _should_run_in_workflow(self, task: PregelExecutableTask) -> bool:
        """
        Determine if task should run in workflow or as activity.

        Args:
            task: The Pregel task to evaluate

        Returns:
            True if task should run in workflow (deterministic),
            False if task should run as activity (non-deterministic)
        """

        # Check if node is marked as workflow-safe
        node = self.pregel.nodes.get(task.name)
        if node and hasattr(node, '_temporal_workflow_safe'):
            return node._temporal_workflow_safe

        # Default: execute as activity for safety
        return False

    async def _execute_in_workflow(
        self,
        task: PregelExecutableTask
    ) -> list[tuple[str, Any]]:
        """
        Execute task directly in workflow (for deterministic operations).

        Args:
            task: The task to execute

        Returns:
            List of (channel, value) tuples representing writes
        """
        from collections import deque
        from langgraph._internal._constants import CONFIG_KEY_SEND

        # Setup write capture
        writes: deque[tuple[str, Any]] = deque()

        # Inject write callback into config
        config = {
            **task.config,
            "configurable": {
                **task.config.get("configurable", {}),
                CONFIG_KEY_SEND: writes.extend,
            },
        }

        # Execute directly - this is safe because it's deterministic
        await task.proc.ainvoke(task.input, config)

        return list(writes)

    async def _execute_as_activity(
        self,
        task: PregelExecutableTask
    ) -> list[tuple[str, Any]]:
        """
        Execute task as Temporal activity with per-node configuration.

        Configuration is resolved in priority order:
        1. Runtime config (task.config)
        2. Node metadata (node.metadata["temporal"])
        3. Node retry_policy (task.retry_policy)
        4. Compile defaults (self.default_*)
        5. System defaults

        Args:
            task: The task to execute

        Returns:
            List of (channel, value) tuples representing writes

        The activity will:
        1. Get the node from the cached graph (via graph_id)
        2. Execute the node (bound + writers)
        3. Capture writes via CONFIG_KEY_SEND callback
        4. Return writes to workflow
        """
        # Get node from compiled graph
        node = self.pregel.nodes.get(task.name)

        # Extract Temporal-specific config from node metadata
        node_temporal_config = {}
        if node and node.metadata:
            node_temporal_config = node.metadata.get("temporal", {})

        # Resolve activity timeout (priority: runtime > metadata > default)
        activity_timeout = (
            task.config.get("metadata", {}).get("temporal_activity_timeout")
            or node_temporal_config.get("activity_timeout")
            or self.default_activity_timeout
        )

        # Resolve task queue (priority: metadata > default > workflow's queue)
        task_queue = (
            node_temporal_config.get("task_queue")
            or self.default_task_queue
        )

        # Resolve heartbeat timeout from metadata
        heartbeat_timeout = node_temporal_config.get("heartbeat_timeout")

        # Build Temporal retry policy from LangGraph's retry_policy
        temporal_retry = self._build_temporal_retry_policy(task.retry_policy)

        # Filter config to serializable parts
        config_dict = self._filter_config(task.config)

        # Generate unique activity ID
        activity_id = (
            f"{task.name}_"
            f"{self._step_counter}_"
            f"{workflow.info().workflow_id}"
        )

        # Build activity input using single Pydantic model (V3.1 update)
        from temporalio.contrib.langgraph.models import NodeActivityInput

        activity_input = NodeActivityInput(
            node_name=task.name,
            task_id=task.id,
            graph_id=self.graph_id,  # V3.1: Use graph_id instead of path
            input_state=task.input,
            config=config_dict,
            path=task.path,
            triggers=list(task.triggers),
        )

        # Execute as activity with resolved configuration
        result = await workflow.execute_activity(
            "execute_langgraph_node",
            activity_input,
            start_to_close_timeout=activity_timeout,
            retry_policy=temporal_retry,
            activity_id=activity_id,
            task_queue=task_queue,
            heartbeat_timeout=heartbeat_timeout,
        )

        # Return writes (V3: Use to_write_tuples() to reconstruct LangChain messages)
        return result.to_write_tuples()

    def _build_temporal_retry_policy(
        self,
        langgraph_policies: Sequence[RetryPolicy] | None
    ) -> TemporalRetryPolicy:
        """
        Map LangGraph's RetryPolicy to Temporal's RetryPolicy.

        LangGraph supports a sequence of retry policies where the first matching
        policy is applied. For Temporal, we use the first policy in the sequence.

        Args:
            langgraph_policies: LangGraph retry policy or sequence of policies

        Returns:
            Temporal retry policy
        """
        # Use first policy if sequence provided
        if langgraph_policies:
            if isinstance(langgraph_policies, (list, tuple)):
                policy = langgraph_policies[0]
            else:
                policy = langgraph_policies

            return TemporalRetryPolicy(
                initial_interval=timedelta(seconds=policy.initial_interval),
                backoff_coefficient=policy.backoff_factor,
                maximum_interval=timedelta(seconds=policy.max_interval),
                maximum_attempts=policy.max_attempts,
                # Note: LangGraph's retry_on and jitter are not mapped
                # Temporal has different retry semantics
            )

        # Use default
        return TemporalRetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_attempts=self.default_max_retries,
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=60),
        )

    def _filter_config(self, config: dict) -> dict:
        """
        Filter config to only serializable parts.

        Args:
            config: Full RunnableConfig

        Returns:
            Dict with only serializable fields
        """
        return {
            "tags": config.get("tags", []),
            "metadata": config.get("metadata", {}),
            "recursion_limit": config.get("recursion_limit"),
            "max_concurrency": config.get("max_concurrency"),
            # Note: callbacks, run_name, run_id are intentionally omitted
            # as they contain non-serializable objects
        }
```

---

#### **5.2.2 Activity Data Models (V3 Update)**

**File:** `temporalio/contrib/langgraph/models.py`

**CRITICAL:** These models solve the type preservation problem for LangChain messages.

```python
"""Data models for LangGraph-Temporal activity interface"""

from typing import Any
from pydantic import BaseModel, ConfigDict
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage


# Message type map for reconstruction
MESSAGE_TYPE_MAP: dict[str, type[BaseMessage]] = {
    "ai": AIMessage,
    "human": HumanMessage,
    "system": SystemMessage,
}


def reconstruct_message(data: dict[str, Any]) -> BaseMessage:
    """Reconstruct a LangChain message from its serialized dict.

    LangChain messages have a "type" field that identifies the message type.
    This is used to reconstruct the proper class from serialized data.
    """
    msg_type = data.get("type")
    msg_cls = MESSAGE_TYPE_MAP.get(msg_type)
    if msg_cls:
        return msg_cls.model_validate(data)
    raise ValueError(f"Unknown message type: {msg_type}")


class ChannelWrite(BaseModel):
    """Represents a write to a LangGraph channel with type preservation.

    CRITICAL: This model solves the type erasure problem where LangChain
    messages in Any-typed fields become plain dicts after serialization.

    The value_type field records whether the value contains LangChain
    messages, enabling proper reconstruction after Temporal serialization.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    channel: str
    value: Any
    value_type: str | None = None  # "message", "message_list", or None

    @classmethod
    def create(cls, channel: str, value: Any) -> "ChannelWrite":
        """Factory that automatically detects and records message types."""
        value_type = None
        if isinstance(value, BaseMessage):
            value_type = "message"
        elif isinstance(value, list) and value and isinstance(value[0], BaseMessage):
            value_type = "message_list"
        return cls(channel=channel, value=value, value_type=value_type)

    def reconstruct_value(self) -> Any:
        """Reconstruct LangChain messages from serialized dicts."""
        if self.value_type == "message" and isinstance(self.value, dict):
            return reconstruct_message(self.value)
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [
                reconstruct_message(item) if isinstance(item, dict) else item
                for item in self.value
            ]
        return self.value

    def to_tuple(self) -> tuple[str, Any]:
        """Convert to (channel, value) tuple with reconstructed messages."""
        return (self.channel, self.reconstruct_value())


class NodeActivityInput(BaseModel):
    """Single Pydantic model for all node activity input data.

    Using a single model instead of multiple parameters provides:
    - Better type safety and validation
    - Cleaner activity signatures
    - Easier testing and mocking
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_name: str                      # Node to execute
    task_id: str                        # Unique task ID from PregelExecutableTask
    graph_id: str                       # V3.1: Graph ID for registry lookup
    input_state: dict[str, Any]         # State to pass to node
    config: dict[str, Any]              # Filtered RunnableConfig
    path: tuple[str | int, ...]         # Graph hierarchy path
    triggers: list[str]                 # Channels that triggered this task


class NodeActivityOutput(BaseModel):
    """Single Pydantic model for node activity output.

    Uses ChannelWrite to preserve LangChain message types through
    Temporal serialization.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    writes: list[ChannelWrite]

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert to list of (channel, value) tuples for Pregel loop."""
        return [w.to_tuple() for w in self.writes]
```

---

#### **5.2.3 LangGraphPlugin (V3.1 NEW)**

**File:** `temporalio/contrib/langgraph/_plugin.py`

```python
"""LangGraph plugin for Temporal integration."""

import threading
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.plugin import SimplePlugin
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
import dataclasses

from langgraph.pregel import Pregel


# Global graph registry - shared across activity invocations
_GRAPH_REGISTRY: dict[str, Callable[[], Pregel]] = {}
_GRAPH_CACHE: dict[str, Pregel] = {}
_CACHE_LOCK = threading.Lock()


def get_graph(graph_id: str) -> Pregel:
    """Get cached compiled graph by ID.

    Thread-safe: uses locking for cache access.
    Graphs are built once per worker process and cached.
    """
    with _CACHE_LOCK:
        if graph_id in _GRAPH_CACHE:
            return _GRAPH_CACHE[graph_id]

        if graph_id not in _GRAPH_REGISTRY:
            raise KeyError(
                f"Graph '{graph_id}' not found. "
                f"Available: {list(_GRAPH_REGISTRY.keys())}"
            )

        # Build and cache
        builder = _GRAPH_REGISTRY[graph_id]
        graph = builder()
        _GRAPH_CACHE[graph_id] = graph
        return graph


def _register_graph(graph_id: str, builder: Callable[[], Pregel]) -> None:
    """Register a graph builder (internal)."""
    _GRAPH_REGISTRY[graph_id] = builder


def _langgraph_data_converter(converter: DataConverter | None) -> DataConverter:
    """Configure data converter for LangGraph serialization."""
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


class LangGraphPlugin(SimplePlugin):
    """Temporal plugin for LangGraph integration.

    This plugin provides seamless integration between LangGraph and Temporal:
    1. Registers graph builders by ID
    2. Auto-registers node execution activities
    3. Configures Pydantic data converter for serialization
    4. Caches compiled graphs per worker process (thread-safe)

    Example:
        >>> from temporalio.client import Client
        >>> from temporalio.worker import Worker
        >>> from temporalio.contrib.langgraph import LangGraphPlugin
        >>>
        >>> # Define graph builders at module level
        >>> def build_weather_agent():
        ...     graph = StateGraph(AgentState)
        ...     # ... add nodes and edges ...
        ...     return graph.compile()
        >>>
        >>> def build_research_agent():
        ...     graph = StateGraph(ResearchState)
        ...     # ... add nodes and edges ...
        ...     return graph.compile()
        >>>
        >>> # Create plugin with registered graphs
        >>> plugin = LangGraphPlugin(
        ...     graphs={
        ...         "weather_agent": build_weather_agent,
        ...         "research_agent": build_research_agent,
        ...     },
        ...     default_activity_timeout=timedelta(minutes=5),
        ... )
        >>>
        >>> # Use with client - activities auto-registered
        >>> client = await Client.connect("localhost:7233", plugins=[plugin])
        >>> worker = Worker(
        ...     client,
        ...     task_queue="langgraph-workers",
        ...     workflows=[MyAgentWorkflow],
        ... )
    """

    def __init__(
        self,
        graphs: dict[str, Callable[[], Pregel]],
        default_activity_timeout: timedelta = timedelta(minutes=5),
        default_max_retries: int = 3,
    ) -> None:
        """Initialize LangGraph plugin.

        Args:
            graphs: Mapping of graph_id to builder function.
                Builder functions should return a compiled Pregel graph.
                Example: {"my_agent": build_my_agent}
            default_activity_timeout: Default timeout for node activities.
                Can be overridden per-node via metadata.
            default_max_retries: Default retry attempts for activities.
        """
        self._graphs = graphs
        self.default_activity_timeout = default_activity_timeout
        self.default_max_retries = default_max_retries

        # Register graphs in global registry
        for graph_id, builder in graphs.items():
            _register_graph(graph_id, builder)

        def add_activities(
            activities: Sequence[Callable] | None,
        ) -> Sequence[Callable]:
            """Add LangGraph node execution activity."""
            from temporalio.contrib.langgraph.activities import (
                NodeExecutionActivity,
            )

            # Create activity instance with access to this plugin
            node_activity = NodeExecutionActivity(self)
            return list(activities or []) + [node_activity.execute_node]

        super().__init__(
            name="LangGraphPlugin",
            data_converter=_langgraph_data_converter,
            activities=add_activities,
        )
```

---

#### **5.2.4 Node Execution Activity**

**File:** `temporalio/contrib/langgraph/activities.py`

```python
"""Temporal activities for LangGraph node execution"""

import asyncio
from collections import deque
from typing import Any, TYPE_CHECKING

from temporalio import activity

from langgraph._internal._constants import CONFIG_KEY_SEND
from temporalio.contrib.langgraph.models import (
    NodeActivityInput,
    NodeActivityOutput,
    ChannelWrite,
)
from temporalio.contrib.langgraph._plugin import get_graph

if TYPE_CHECKING:
    from temporalio.contrib.langgraph._plugin import LangGraphPlugin


class NodeExecutionActivity:
    """Activity class for executing LangGraph nodes.

    Uses the graph registry to get cached compiled graphs.
    """

    def __init__(self, plugin: "LangGraphPlugin") -> None:
        self._plugin = plugin

    @activity.defn(name="execute_langgraph_node")
    async def execute_node(self, input_data: NodeActivityInput) -> NodeActivityOutput:
        """Execute a LangGraph node as a Temporal activity.

        This activity:
        1. Gets the compiled graph from the registry cache
        2. Gets the node's combined runnable (bound + writers)
        3. Captures writes via CONFIG_KEY_SEND callback
        4. Returns writes wrapped in ChannelWrite for type preservation

        Args:
            input_data: NodeActivityInput containing node_name, graph_id, etc.

        Returns:
            NodeActivityOutput with writes as ChannelWrite list
        """
        # Get cached graph from registry (V3.1: no rebuild!)
        graph = get_graph(input_data.graph_id)

        # Get node
        pregel_node = graph.nodes.get(input_data.node_name)
        if not pregel_node:
            available = list(graph.nodes.keys())
            raise ValueError(
                f"Node '{input_data.node_name}' not found in graph '{input_data.graph_id}'. "
                f"Available: {available}"
            )

        # Get combined runnable (bound + writers)
        node_runnable = pregel_node.node
        if not node_runnable:
            return NodeActivityOutput(writes=[])

        # Setup write capture
        writes: deque[tuple[str, Any]] = deque()

        # Inject write callback into config
        config = {
            **input_data.config,
            "configurable": {
                **input_data.config.get("configurable", {}),
                CONFIG_KEY_SEND: writes.extend,
            }
        }

        # Send heartbeat
        activity.heartbeat({
            "node": input_data.node_name,
            "task_id": input_data.task_id,
            "graph_id": input_data.graph_id,
            "status": "executing"
        })

        # Execute node
        if asyncio.iscoroutinefunction(node_runnable.invoke):
            await node_runnable.ainvoke(input_data.input_state, config)
        else:
            node_runnable.invoke(input_data.input_state, config)

        # Send completion heartbeat
        activity.heartbeat({
            "node": input_data.node_name,
            "task_id": input_data.task_id,
            "graph_id": input_data.graph_id,
            "status": "completed",
            "writes": len(writes)
        })

        # Convert writes to ChannelWrite for type preservation
        channel_writes = [
            ChannelWrite.create(channel, value)
            for channel, value in writes
        ]

        return NodeActivityOutput(writes=channel_writes)
```

---

#### **5.2.5 Package Initialization**

**File:** `temporalio/contrib/langgraph/__init__.py`

```python
"""Temporal integration for LangGraph"""

from datetime import timedelta
from typing import Optional

from temporalio.contrib.langgraph._plugin import LangGraphPlugin, get_graph
from temporalio.contrib.langgraph.runner import TemporalLangGraphRunner


def compile(
    graph_id: str,
    *,
    default_activity_timeout: Optional[timedelta] = None,
    default_max_retries: int = 3,
    default_task_queue: Optional[str] = None,
) -> TemporalLangGraphRunner:
    """
    Compile a registered LangGraph graph for Temporal execution.

    V3.1 API: Takes graph_id instead of graph object. The graph must be
    registered with LangGraphPlugin before calling this function.

    This provides a clean API where:
    - Graphs are registered via LangGraphPlugin(graphs={...})
    - graph_id is passed as workflow parameter
    - Activities use cached graphs from the registry

    Configuration Priority (highest to lowest):
    1. Runtime config passed to ainvoke(config={...})
    2. Node metadata: metadata={"temporal": {...}}
    3. Node retry_policy: retry_policy=RetryPolicy(...)
    4. Compile defaults (these parameters)
    5. System defaults

    Args:
        graph_id: ID of the graph registered with LangGraphPlugin.
            This should match a key in the `graphs` dict passed to the plugin.
        default_activity_timeout: Default timeout for node activities.
            Can be overridden per-node via metadata.
            Default: 5 minutes
        default_max_retries: Default maximum retry attempts.
            Can be overridden per-node via retry_policy.
            Default: 3
        default_task_queue: Default task queue for activities.
            Can be overridden per-node via metadata.
            Default: None (uses workflow's task queue)

    Returns:
        TemporalLangGraphRunner that can be used like a compiled graph

    Example:
        Setup (main.py):
        >>> from temporalio.client import Client
        >>> from temporalio.contrib.langgraph import LangGraphPlugin
        >>>
        >>> # Define builders at module level (myapp/agents.py)
        >>> def build_weather_agent():
        ...     graph = StateGraph(AgentState)
        ...     graph.add_node("fetch", fetch_data)
        ...     graph.add_node("process", process_data)
        ...     return graph.compile()
        >>>
        >>> # Create plugin with registered graphs
        >>> plugin = LangGraphPlugin(
        ...     graphs={"weather_agent": build_weather_agent}
        ... )
        >>> client = await Client.connect("localhost:7233", plugins=[plugin])

        Usage (workflow.py):
        >>> from temporalio.contrib.langgraph import compile
        >>>
        >>> @workflow.defn
        >>> class WeatherAgentWorkflow:
        ...     @workflow.run
        ...     async def run(self, graph_id: str, query: str):
        ...         # graph_id comes from workflow input
        ...         app = compile(graph_id)
        ...         return await app.ainvoke({"query": query})

        Execution:
        >>> await client.execute_workflow(
        ...     WeatherAgentWorkflow.run,
        ...     args=["weather_agent", "What's the weather?"],
        ...     id="weather-1",
        ...     task_queue="langgraph-workers",
        ... )
    """
    # Get graph from registry
    pregel = get_graph(graph_id)

    return TemporalLangGraphRunner(
        pregel,
        graph_id=graph_id,
        default_activity_timeout=default_activity_timeout,
        default_max_retries=default_max_retries,
        default_task_queue=default_task_queue,
    )


__all__ = [
    "compile",
    "LangGraphPlugin",
    "TemporalLangGraphRunner",
]
```

---

### **5.3 Configuration Guide**

The Temporal LangGraph integration provides flexible configuration at multiple levels, leveraging LangGraph's native configuration mechanisms.

#### **5.3.1 Configuration Levels**

Configuration is resolved in priority order (highest to lowest):

1. **Runtime Configuration** - Passed to `ainvoke(config={...})`
2. **Node Metadata** - Set when adding nodes via `metadata={"temporal": {...}}`
3. **Node Retry Policy** - Set when adding nodes via `retry_policy=RetryPolicy(...)`
4. **Compile Defaults** - Set via `compile(default_activity_timeout=...)`
5. **System Defaults** - Hardcoded fallbacks

#### **5.3.2 Available Configuration Options**

##### **Activity Timeout**

Controls how long an activity can run before timing out.

```python
from datetime import timedelta

# Level 1: Runtime (highest priority)
result = await app.ainvoke(
    input_data,
    config={"metadata": {"temporal_activity_timeout": timedelta(minutes=10)}}
)

# Level 2: Node metadata
graph.add_node(
    "slow_node",
    slow_node,
    metadata={"temporal": {"activity_timeout": timedelta(hours=1)}}
)

# Level 4: Compile default
app = compile(graph, default_activity_timeout=timedelta(minutes=5))

# Level 5: System default = 5 minutes
```

##### **Retry Policy**

Controls how activities are retried on failure. Uses LangGraph's native `RetryPolicy`.

```python
from langgraph.types import RetryPolicy

# Level 2: Node retry_policy (LangGraph native!)
graph.add_node(
    "flaky_api",
    flaky_api,
    retry_policy=RetryPolicy(
        initial_interval=1.0,      # Seconds before first retry
        backoff_factor=2.0,         # Exponential backoff multiplier
        max_interval=60.0,          # Max seconds between retries
        max_attempts=5,             # Total attempts including first
    )
)

# Level 4: Compile default (max_attempts only)
app = compile(graph, default_max_retries=3)

# Level 5: System default = 3 attempts with exponential backoff
```

**Mapping to Temporal:**
- `initial_interval` → `initial_interval`
- `backoff_factor` → `backoff_coefficient`
- `max_interval` → `maximum_interval`
- `max_attempts` → `maximum_attempts`

##### **Task Queue**

Route specific nodes to specialized workers (e.g., GPU workers, high-memory workers).

```python
# Level 2: Node metadata
graph.add_node(
    "gpu_processing",
    gpu_processing,
    metadata={"temporal": {"task_queue": "gpu-workers"}}
)

graph.add_node(
    "memory_intensive",
    memory_intensive,
    metadata={"temporal": {"task_queue": "highmem-workers"}}
)

# Level 4: Compile default (applies to all nodes without explicit queue)
app = compile(graph, default_task_queue="standard-workers")

# Level 5: System default = None (uses workflow's task queue)
```

##### **Heartbeat Timeout**

For long-running activities that need to report progress.

```python
# Level 2: Node metadata only
graph.add_node(
    "long_running",
    long_running,
    metadata={
        "temporal": {
            "activity_timeout": timedelta(hours=2),
            "heartbeat_timeout": timedelta(minutes=5),  # Activity must heartbeat
        }
    }
)
```

##### **Hybrid Execution**

Run deterministic nodes directly in workflow instead of activities.

```python
from temporalio.contrib.langgraph import temporal_node_metadata

# Level 2: Node metadata (using helper function)
graph.add_node(
    "validate_input",  # Deterministic validation
    validate_input,
    metadata=temporal_node_metadata(run_in_workflow=True),
)

# Level 4: Compile - nodes with run_in_workflow=True execute in workflow
app = compile(graph)
# Nodes opt-in via metadata to run in workflow

# Level 5: System default = False (all nodes run as activities)
```

#### **5.3.3 Complete Configuration Example**

```python
from datetime import timedelta
from langgraph.graph import StateGraph, START
from langgraph.types import RetryPolicy
from temporalio.contrib.langgraph import (
    compile,
    node_activity_options,
    temporal_node_metadata,
)

# Build graph with comprehensive per-node configuration
graph = StateGraph(MyState)

# Fast deterministic validation - runs in workflow
graph.add_node(
    "validate",
    validate_input,
    metadata=temporal_node_metadata(run_in_workflow=True),
)

# External API with retries and timeout
graph.add_node(
    "fetch_weather",
    fetch_weather,
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_interval=1.0,
        backoff_factor=2.0,
        max_interval=30.0,
    ),
    metadata=node_activity_options(
        start_to_close_timeout=timedelta(minutes=2),
        heartbeat_timeout=timedelta(seconds=30),
    ),
)

# GPU-intensive processing on specialized workers
graph.add_node(
    "process_image",
    process_image,
    retry_policy=RetryPolicy(max_attempts=2),  # Don't retry too much
    metadata=node_activity_options(
        start_to_close_timeout=timedelta(hours=1),
        task_queue="gpu-workers",
        heartbeat_timeout=timedelta(minutes=10),
    ),
)

# Standard processing with defaults
graph.add_node("finalize", finalize_result)

graph.add_edge(START, "validate")
graph.add_edge("validate", "fetch_weather")
graph.add_edge("fetch_weather", "process_image")
graph.add_edge("process_image", "finalize")

# Compile with defaults for unconfigured nodes
app = compile(
    graph,
    default_activity_timeout=timedelta(minutes=5),
    default_max_retries=3,
    default_task_queue="standard-workers",
)

# Execute with runtime override
result = await app.ainvoke(
    input_data,
    config={
        "metadata": {
            # Override timeout for this specific execution
            "temporal_activity_timeout": timedelta(minutes=10)
        }
    }
)
```

#### **5.3.4 Configuration Best Practices**

1. **Use compile defaults** for standard policies that apply to most nodes
2. **Use node metadata** for node-specific requirements (timeouts, task queues)
3. **Use retry_policy** for failure handling (LangGraph native)
4. **Use runtime config** sparingly for execution-specific overrides
5. **Hybrid execution** should be opt-in per node, not enabled globally for all nodes

#### **5.3.5 Configuration Reference**

| Setting | Node Metadata Key | Compile Parameter | Runtime Config Key | Default |
|---------|------------------|-------------------|-------------------|---------|
| Activity Timeout | `temporal.activity_timeout` | `default_activity_timeout` | `metadata.temporal_activity_timeout` | 5 min |
| Retry Policy | (use `retry_policy` param) | `default_max_retries` | N/A | 3 attempts |
| Task Queue | `temporal.task_queue` | `default_task_queue` | N/A | workflow queue |
| Heartbeat | `temporal.heartbeat_timeout` | N/A | N/A | None |
| Hybrid Exec | `temporal.run_in_workflow` | N/A | N/A | False |

#### **5.3.6 Helper Functions**

The SDK provides two helper functions for creating node metadata with proper typing and structure.

##### **node_activity_options()**

Creates activity-specific configuration for nodes. Use this for timeouts, retries, task queues, and other activity settings.

```python
from datetime import timedelta
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import node_activity_options

# Basic timeout configuration
graph.add_node(
    "fetch_data",
    fetch_data,
    metadata=node_activity_options(
        start_to_close_timeout=timedelta(minutes=5),
    ),
)

# Full activity configuration
graph.add_node(
    "process",
    process_data,
    metadata=node_activity_options(
        start_to_close_timeout=timedelta(minutes=30),
        heartbeat_timeout=timedelta(minutes=5),
        task_queue="gpu-workers",
        retry_policy=RetryPolicy(
            maximum_attempts=5,
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
        ),
    ),
)
```

##### **temporal_node_metadata()**

Higher-level helper that combines activity options with workflow execution flags. Use this when you need to specify `run_in_workflow` along with activity options.

```python
from temporalio.contrib.langgraph import temporal_node_metadata, node_activity_options

# Mark a node to run in workflow (deterministic operations)
graph.add_node(
    "validate",
    validate_input,
    metadata=temporal_node_metadata(run_in_workflow=True),
)

# Combine activity options with run_in_workflow
graph.add_node(
    "transform",
    transform_data,
    metadata=temporal_node_metadata(
        activity_options=node_activity_options(
            start_to_close_timeout=timedelta(minutes=10),
            task_queue="compute-workers",
        ),
        run_in_workflow=False,  # Run as activity (default)
    ),
)
```

**When to use which:**

| Scenario | Use |
|----------|-----|
| Activity configuration only | `node_activity_options()` |
| `run_in_workflow=True` only | `temporal_node_metadata(run_in_workflow=True)` |
| Both activity options and `run_in_workflow` | `temporal_node_metadata(activity_options=..., run_in_workflow=...)` |
| Raw metadata dict | `metadata={"temporal": {...}}` |

---

## **6. Usage Examples**

### **6.1 Basic Example**

```python
import uuid
import asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition
from temporalio.contrib.langgraph import compile, LangGraphPlugin

# 1. Define tools (at module level - must be importable!)
@tool
def get_weather(city: str) -> str:
    """Get weather for a city"""
    import requests
    data = requests.get(f"https://api.weather.com/{city}").json()
    return f"Weather in {city}: {data['temp']}°F"

@tool
def calculator(expression: str) -> float:
    """Evaluate a math expression"""
    return eval(expression)

# 2. Define graph builder (at module level!)
def build_weather_agent():
    """Build the weather agent graph"""
    tools = [get_weather, calculator]

    graph = StateGraph(dict)
    # V3.1: Lambdas work! Graph is cached per worker, not rebuilt per activity
    graph.add_node("agent", lambda state: {
        "messages": ChatOpenAI(model="gpt-4").bind_tools(tools).invoke(
            state["messages"]
        )
    })
    graph.add_node("tools", ToolNode(tools))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    return graph.compile()

# 3. Define workflow - graph_id is now a parameter
@workflow.defn
class WeatherAgentWorkflow:
    @workflow.run
    async def run(self, graph_id: str, user_question: str) -> str:
        # V3.1: Just use graph_id - graph comes from plugin registry
        app = compile(graph_id)

        # Execute
        result = await app.ainvoke({
            "messages": [("user", user_question)]
        })

        return result["messages"][-1].content

# 4. Setup plugin, worker and execute
async def main():
    # V3.1: Create plugin with registered graphs
    plugin = LangGraphPlugin(
        graphs={
            "weather_agent": build_weather_agent,  # Register by ID
        }
    )

    # Plugin registered on client - activities auto-registered
    client = await Client.connect("localhost:7233", plugins=[plugin])

    # Worker inherits plugin config - no need to specify activities!
    worker = Worker(
        client,
        task_queue="langgraph-agents",
        workflows=[WeatherAgentWorkflow],
        # activities auto-registered via plugin
    )

    async with worker:
        # Execute workflow with graph_id
        result = await client.execute_workflow(
            WeatherAgentWorkflow.run,
            args=["weather_agent", "What's the weather in San Francisco?"],
            id=f"weather-agent-{uuid.uuid4()}",
            task_queue="langgraph-agents",
        )

        print(result)

if __name__ == "__main__":
    asyncio.run(main())
```

### **6.2 With Per-Node Configuration**

```python
from datetime import timedelta
from langgraph.types import RetryPolicy
from langgraph.graph import StateGraph, START
from temporalio.contrib.langgraph import compile, LangGraphPlugin

def build_data_pipeline():
    """Build pipeline with different requirements per node"""

    graph = StateGraph(dict)

    # Fast validation - no special config needed
    graph.add_node("validate", validate_input)

    # External API - needs retries and timeout
    graph.add_node(
        "fetch_data",
        fetch_from_api,
        retry_policy=RetryPolicy(
            max_attempts=5,
            initial_interval=1.0,
            backoff_factor=2.0,
        ),
        metadata={
            "temporal": {
                "activity_timeout": timedelta(minutes=2),
                "heartbeat_timeout": timedelta(seconds=30),
            }
        }
    )

    # Heavy GPU processing - special queue and long timeout
    graph.add_node(
        "process_gpu",
        process_with_gpu,
        retry_policy=RetryPolicy(max_attempts=2),
        metadata={
            "temporal": {
                "activity_timeout": timedelta(hours=1),
                "task_queue": "gpu-workers",
                "heartbeat_timeout": timedelta(minutes=10),
            }
        }
    )

    # Final aggregation
    graph.add_node("aggregate", aggregate_results)

    graph.add_edge(START, "validate")
    graph.add_edge("validate", "fetch_data")
    graph.add_edge("fetch_data", "process_gpu")
    graph.add_edge("process_gpu", "aggregate")

    return graph.compile()

@workflow.defn
class DataPipelineWorkflow:
    @workflow.run
    async def run(self, graph_id: str, input_data: dict):
        # V3.1: Use graph_id parameter
        app = compile(
            graph_id,
            default_activity_timeout=timedelta(minutes=5),
            default_max_retries=3,
        )

        return await app.ainvoke(input_data)

# Setup in main.py
plugin = LangGraphPlugin(
    graphs={"data_pipeline": build_data_pipeline}
)
```

### **6.3 With Prebuilt Agents**

**Using create_agent (LangChain 1.0+, Recommended):**

```python
from langchain.agents import create_agent
from temporalio.contrib.langgraph import compile, LangGraphPlugin

def build_agent():
    """Build an agent using LangChain 1.0+ API"""
    return create_agent(
        model="openai:gpt-4",
        tools=[search_web, calculator, file_reader]
    )

@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, graph_id: str, task: str):
        app = compile(graph_id)

        return await app.ainvoke({
            "messages": [("user", task)]
        })

# Setup in main.py
plugin = LangGraphPlugin(
    graphs={"my_agent": build_agent}
)
```

**Using create_react_agent (LangGraph Prebuilt, Legacy):**

```python
from langgraph.prebuilt import create_react_agent
from temporalio.contrib.langgraph import compile, LangGraphPlugin

def build_react_agent():
    """Build a ReAct agent (legacy API)"""
    return create_react_agent(
        ChatOpenAI(model="gpt-4"),
        tools=[search_web, calculator, file_reader]
    )

@workflow.defn
class ReactAgentWorkflow:
    @workflow.run
    async def run(self, graph_id: str, task: str):
        # V3.1: Prebuilt agents work seamlessly with graph_id
        app = compile(graph_id)

        return await app.ainvoke({
            "messages": [("user", task)]
        })

# Setup in main.py
plugin = LangGraphPlugin(
    graphs={"react_agent": build_react_agent}
)
```

### **6.4 With Hybrid Execution (Optimization)**

For deterministic operations (validation, routing, pure computations), you can run them directly in the workflow instead of activities for better performance.

```python
from langgraph.graph import StateGraph, START
from temporalio.contrib.langgraph import compile, LangGraphPlugin

# Deterministic operations
def validate_input(state: dict) -> dict:
    """Pure validation - no I/O"""
    if not state.get("user_id"):
        raise ValueError("user_id required")
    return {"validated": True}

def route_by_priority(state: dict) -> str:
    """Deterministic routing logic"""
    priority = state.get("priority", "normal")
    return "fast_track" if priority == "high" else "standard"

def transform_data(state: dict) -> dict:
    """Pure computation - no I/O"""
    return {
        "processed": [x * 2 for x in state["data"]]
    }

# Non-deterministic operation
def fetch_from_api(state: dict) -> dict:
    """I/O operation - must run as activity"""
    import requests
    data = requests.get(state["url"]).json()
    return {"data": data}

def build_hybrid_graph():
    from temporalio.contrib.langgraph import temporal_node_metadata

    graph = StateGraph(dict)

    # Fast deterministic nodes - run in workflow
    graph.add_node(
        "validate",
        validate_input,
        metadata=temporal_node_metadata(run_in_workflow=True),
    )

    graph.add_node(
        "transform",
        transform_data,
        metadata=temporal_node_metadata(run_in_workflow=True),
    )

    # I/O node - runs as activity
    graph.add_node("fetch", fetch_from_api)

    graph.add_edge(START, "validate")
    graph.add_edge("validate", "fetch")
    graph.add_edge("fetch", "transform")

    return graph.compile()

@workflow.defn
class HybridWorkflow:
    @workflow.run
    async def run(self, graph_id: str, url: str):
        # V3.1: Use graph_id, enable hybrid execution
        app = compile(graph_id)

        return await app.ainvoke({"url": url})

# Setup in main.py
plugin = LangGraphPlugin(
    graphs={"hybrid_graph": build_hybrid_graph}
)
```

**Note:** Hybrid execution is opt-in per node via metadata. Only use for truly deterministic operations!

### **6.5 With Child Workflows (Nested Graphs)**

For complex nested graphs, you can execute subgraphs as child workflows for better isolation and scalability.

```python
from temporalio import workflow
from temporalio.contrib.langgraph import compile, LangGraphPlugin

# Child graph workflow
@workflow.defn
class DataProcessorWorkflow:
    @workflow.run
    async def run(self, graph_id: str, data: list):
        # V3.1: Use graph_id
        app = compile(graph_id)
        return await app.ainvoke({"data": data})

# Parent uses child workflow
async def process_with_child(state: dict) -> dict:
    """Node that invokes child workflow for subgraph"""
    result = await workflow.execute_child_workflow(
        DataProcessorWorkflow.run,
        args=["processor_graph", state["data"]],  # Pass graph_id to child
        id=f"processor-{state['id']}"
    )
    return {"processed_data": result}

def build_processor_graph():
    # ... define processor subgraph ...
    return graph.compile()

def build_parent_graph():
    graph = StateGraph(dict)
    graph.add_node("fetch", fetch_data)
    graph.add_node("process", process_with_child)  # Executes child workflow
    graph.add_node("finalize", finalize_result)

    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "process")
    graph.add_edge("process", "finalize")

    return graph.compile()

@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self, graph_id: str, input_data: dict):
        # V3.1: Use graph_id
        app = compile(graph_id)
        return await app.ainvoke(input_data)

# Setup in main.py - register both graphs
plugin = LangGraphPlugin(
    graphs={
        "parent_graph": build_parent_graph,
        "processor_graph": build_processor_graph,
    }
)
```

---

## **7. Testing Strategy**

### **7.1 Unit Tests**

```python
# test_runner.py
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

@pytest.mark.asyncio
async def test_simple_graph():
    """Test simple graph execution"""
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test",
            workflows=[SimpleWorkflow],
            activities=[execute_langgraph_node],
        ):
            result = await env.client.execute_workflow(
                SimpleWorkflow.run,
                "test input",
                id="test-1",
                task_queue="test",
            )

            assert result["output"] == "expected"

@pytest.mark.asyncio
async def test_error_handling():
    """Test activity failure and retry"""
    # Mock activity to fail first 2 times
    # Verify retry policy works

@pytest.mark.asyncio
async def test_write_capture():
    """Test that writes are captured correctly"""
    # Verify activity returns correct writes
    # Verify workflow state updates properly
```

### **7.2 Integration Tests**

```python
# test_integration.py

@pytest.mark.asyncio
async def test_react_agent():
    """Test with prebuilt ReAct agent"""
    # Test full agent execution
    # Verify tool calls work
    # Verify state management

@pytest.mark.asyncio
async def test_multi_step_agent():
    """Test agent with multiple tool calls"""
    # Verify orchestration across steps
    # Verify state accumulation

@pytest.mark.asyncio
async def test_workflow_replay():
    """Test workflow replay after failure"""
    # Simulate failure mid-execution
    # Verify replay completes correctly
```

### **7.3 Performance Tests**

```python
@pytest.mark.asyncio
async def test_hybrid_execution_performance():
    """Compare all-activity vs hybrid execution"""
    # Measure execution time
    # Measure activity count
    # Verify hybrid is faster for pure nodes

@pytest.mark.asyncio
async def test_large_state():
    """Test with large state objects"""
    # Verify serialization performance
    # Verify memory usage
```

---

## **8. Migration and Compatibility**

### **8.1 From Standalone LangGraph**

**Before (standalone LangGraph):**
```python
graph = StateGraph(State)
# ... define graph ...
app = graph.compile()
result = app.invoke(input)
```

**After (with Temporal, V3.1):**
```python
# 1. Define builder function (can be same file or separate module)
def build_my_graph():
    graph = StateGraph(State)
    # ... define graph (same as before!) ...
    return graph.compile()

# 2. Create plugin and client
plugin = LangGraphPlugin(graphs={"my_graph": build_my_graph})
client = await Client.connect("localhost:7233", plugins=[plugin])

# 3. Define workflow with graph_id parameter
@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, graph_id: str, input: dict):
        app = compile(graph_id)  # Uses registered graph
        return await app.ainvoke(input)

# 4. Start workflow
await client.execute_workflow(
    MyWorkflow.run,
    args=["my_graph", {"query": "test"}],
    id="my-workflow-1",
    task_queue="my-queue",
)
```

### **8.2 Migration Checklist**

- [ ] Move graph definition to module-level builder function that returns `graph.compile()`
- [ ] Create `LangGraphPlugin` with graph ID → builder mapping
- [ ] Register plugin with client: `Client.connect(..., plugins=[plugin])`
- [ ] Add `graph_id` as workflow parameter
- [ ] Use `compile(graph_id)` in workflow instead of direct graph.compile()
- [ ] Remove manual activity registration (plugin handles it)
- [ ] Test with WorkflowEnvironment
- [ ] Deploy to production

**Note:** Lambdas and closures work in V3.1! Graphs are cached per worker process, so lambda references are preserved.

### **8.3 Compatibility Matrix**

| Feature | Supported | Notes |
|---------|-----------|-------|
| **StateGraph** | ✅ Yes | Full support |
| **MessageGraph** | ✅ Yes | Via StateGraph |
| **Regular nodes** | ✅ Yes | Execute as activities |
| **Conditional edges** | ✅ Yes | Evaluate in workflow |
| **Send API** | ✅ Yes | Dynamic tasks supported |
| **ToolNode** | ✅ Yes | Executes as activity |
| **create_agent** (LangChain 1.0+) | ✅ Yes | Full support (recommended) |
| **create_react_agent** (legacy) | ✅ Yes | Full support |
| **Interrupts** | ⚠️  Partial | V1: Basic support, V2: Full signals |
| **Subgraphs** | ⚠️  Partial | V1: Inline, V2: Child workflows |
| **Streaming** | ⚠️  Limited | Queries/heartbeats for progress |
| **Module globals** | ❌ No | Use graph state instead |

---

## **9. Performance Considerations**

### **9.1 Optimization Strategies**

**1. Hybrid Execution (Most Impact)**
```python
# Before: All nodes as activities
runner = TemporalLangGraphRunner(graph)

# After: Pure nodes in workflow
runner = TemporalLangGraphRunner(graph)
# Result: 40-60% fewer activity executions for transform-heavy graphs
```

**2. Activity Batching**
```python
# Future enhancement: Batch multiple pure nodes
runner = TemporalLangGraphRunner(
    graph,
    batch_pure_nodes=True  # Execute multiple transforms together
)
```

**3. Caching**
```python
# Use LangGraph's built-in caching
graph.add_node(
    "expensive_compute",
    expensive_function,
    cache_policy=CachePolicy(ttl=3600)
)
```

### **9.2 Cost Analysis**

**Example: 10-node graph, 5 I/O nodes, 5 transform nodes**

| Configuration | Activity Executions | Cost Impact |
|--------------|---------------------|-------------|
| All activities | 10 | 1.0x (baseline) |
| Hybrid (transforms in workflow) | 5 | 0.5x (50% reduction) |
| With caching | 3-5 | 0.3-0.5x (70% reduction) |

---

## **10. Future Enhancements**

### **V2.0: Advanced Features**

- [ ] **Full interrupt support** via Temporal signals
- [ ] **Subgraph as child workflows** with proper isolation
- [ ] **Streaming via queries** for real-time progress
- [ ] **Automatic determinism detection** for hybrid execution
- [ ] **LangGraph Cloud migration** path

### **V3.0: Enterprise Features**

- [ ] **Multi-tenancy** support
- [ ] **Rate limiting** integration
- [ ] **Cost tracking** and quotas
- [ ] **Advanced observability** with custom metrics
- [ ] **A/B testing** framework for agents

---

## **11. References**

### **11.1 Documentation**

- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [Temporal Python SDK](https://docs.temporal.io/docs/python)
- [Temporal OpenAI Agents Integration](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents)

### **11.2 Key Files**

**LangGraph:**
- `libs/langgraph/langgraph/pregel/main.py` - Pregel core
- `libs/langgraph/langgraph/pregel/_loop.py` - Execution loop
- `libs/langgraph/langgraph/pregel/_algo.py` - Task preparation
- `libs/langgraph/langgraph/pregel/_retry.py` - Retry logic
- `libs/prebuilt/langgraph/prebuilt/tool_node.py` - ToolNode

**Temporal:**
- `temporalio/contrib/openai_agents/workflow.py` - OpenAI pattern
- `temporalio/contrib/openai_agents/_temporal_openai_agents.py` - Runner

---

## **Appendix A: Data Types and Serialization**

### **A.1 Serializable Types**

✅ **Can pass to activities:**
- Primitives: `str`, `int`, `float`, `bool`, `None`
- Collections: `list`, `dict`, `tuple`
- Pydantic models (with `PydanticPayloadConverter`)
- NamedTuples: `RetryPolicy`, `CacheKey`
- State dictionaries (if values are serializable)

### **A.2 Non-Serializable Types**

❌ **Cannot pass to activities:**
- Functions, lambdas, closures
- `Runnable` objects (complex object graphs)
- `deque` (convert to `list`)
- `RunnableConfig` callbacks
- Run managers, context managers

### **A.3 Activity Interface**

```python
@activity.defn
async def execute_langgraph_node(
    node_name: str,           # ✅ String
    input_data: dict,         # ✅ Dict (if values serializable)
    config_dict: dict,        # ✅ Filtered dict
    step: int,                # ✅ Primitive
) -> list[tuple[str, Any]]:   # ✅ List of tuples
    """All parameters and return value must be serializable"""
```

---

## **Appendix B: Implementation Phases**

The implementation is organized into phases, with Phase 1 focused on validating technical assumptions through throwaway prototypes before building the production implementation.

### **Phase 1: Validation & Prototypes**

**Goal:** Validate all technical assumptions with throwaway prototypes and tests before committing to implementation.

**Technical Concerns to Validate:**
1. AsyncPregelLoop API - How to drive graph execution? (Answer: `tick()`/`after_tick()`, NOT `submit`)
2. Write Capture - Does CONFIG_KEY_SEND callback work as described?
3. Task Interface - What is the actual PregelExecutableTask structure?
4. Serialization - Can LangGraph state be serialized for Temporal?
5. Graph Builder - How do activities reconstruct the graph?

**Deliverables:**
- Prototype code in `_prototypes/` directory (throwaway)
- Unit tests validating each assumption
- Validation summary documenting findings
- Updated proposal if any assumptions were incorrect

**Exit Criteria:**
- [x] Confirmed AsyncPregelLoop `tick()`/`after_tick()` pattern works (note: `submit` is NOT for node execution - see section 4.4)
- [x] Confirmed write capture returns correct format
- [x] Documented actual PregelExecutableTask interface
- [x] Identified serialization requirements/limitations
- [x] Chosen graph reconstruction approach (plugin registry with caching)

**Details:** See [Phase 1 Implementation Plan](./langgraph-phase1-validation.md)

---

### **Phase 2: Core Runner**

**Goal:** Implement the core TemporalLangGraphRunner with basic execution.

**Deliverables:**
- `TemporalLangGraphRunner` class with `ainvoke` method
- Pregel loop integration with custom submit function
- Basic activity execution (without full write capture)

**Dependencies:** Phase 1 findings

---

### **Phase 3: Activity & Write Capture**

**Goal:** Implement the node execution activity with proper write capture.

**Deliverables:**
- `execute_langgraph_node` activity
- Write capture mechanism
- Activity-to-workflow state synchronization

**Dependencies:** Phase 2

---

### **Phase 4: Configuration**

**Goal:** Implement per-node configuration and policy mapping.

**Deliverables:**
- Config filtering for serialization
- Per-node timeout/retry via metadata
- Retry policy mapping (LangGraph → Temporal)
- Task queue routing

**Dependencies:** Phase 3

---

### **Phase 5: Hybrid Execution (Optional)**

**Goal:** Enable deterministic nodes to run directly in workflow.

**Deliverables:**
- Deterministic node detection
- Workflow-side execution for pure nodes

**Dependencies:** Phase 4

---

### **Phase 6: Testing**

**Goal:** Comprehensive test coverage.

**Deliverables:**
- Unit tests (runner, activity, config)
- Integration tests (simple graph, ToolNode, ReAct agent)
- Replay/determinism tests
- Performance benchmarks

**Dependencies:** Phase 5

---

### **Phase 7: Examples & Documentation**

**Goal:** Production-ready documentation and examples.

**Deliverables:**
- Basic usage example
- Per-node configuration example
- Prebuilt agent example (ReAct)
- Migration guide from standalone LangGraph
- API documentation

**Dependencies:** Phase 6

---

**End of Document**
