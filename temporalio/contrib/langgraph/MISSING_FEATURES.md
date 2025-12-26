# LangGraph-Temporal Integration: Missing Features Analysis

**Date:** 2025-12-26
**Status:** Phase 1 Complete
**Current Implementation:** Core features implemented and validated

---

## Overview

This document tracks the features for a complete LangGraph-Temporal integration. The current implementation provides comprehensive support for most LangGraph features:

**✅ Implemented:**
- Core Pregel loop execution in workflows
- Node execution as Temporal activities
- Write capture via CONFIG_KEY_SEND
- LangChain message type preservation
- Per-node Temporal configuration (timeouts, task queues, retry policies)
- Parallel execution within ticks (BSP model)
- Conditional edge support
- Plugin-based architecture
- Human-in-the-Loop (interrupt/resume via signals)
- Store (cross-node persistence via ActivityLocalStore)
- Send API / Dynamic Parallelism (map-reduce patterns)
- Command API (goto navigation)
- Subgraphs / Nested Graphs

**🟡 Partial:**
- Checkpointing (continue-as-new pattern available)

**⚪ Out of Scope:**
- Streaming (not planned - use LangGraph directly for streaming use cases)

---

## 🔴 Critical Missing Features

### 1. Human-in-the-Loop / Interrupts

**LangGraph Feature:**
LangGraph's `interrupt()` function allows pausing graph execution at any point to wait for external input. This enables:

- **Approve/Reject**: Pause before critical actions (API calls, tool execution) for human review
- **Edit State**: Allow humans to modify graph state mid-execution
- **Review Tool Calls**: Inspect and modify LLM-requested tool calls before execution
- **Multi-turn Dialogs**: Dynamic, interactive conversations

**Example LangGraph Usage:**
```python
from langgraph.types import interrupt

def human_review_node(state):
    # Pause and wait for human input
    human_input = interrupt({
        "question": "Should I proceed with this action?",
        "proposed_action": state["next_action"],
    })

    if human_input["approved"]:
        return {"status": "approved"}
    else:
        return {"status": "rejected", "reason": human_input["reason"]}
```

**Temporal Mapping:**
```python
# Proposed implementation using Temporal signals
@workflow.defn
class LangGraphWorkflow:
    def __init__(self):
        self._interrupt_response = None
        self._waiting_for_interrupt = False

    @workflow.signal
    def resume_interrupt(self, response: dict):
        self._interrupt_response = response

    @workflow.run
    async def run(self, input_state: dict):
        # When interrupt() is called in a node...
        # The runner would:
        # 1. Save interrupt data to workflow state
        # 2. Wait for signal: await workflow.wait_condition(lambda: self._interrupt_response)
        # 3. Return interrupt response to the node
        pass
```

**Implementation Complexity:** High
**Priority:** Critical - Most requested AI agent feature

---

### 2. Streaming

**LangGraph Feature:**
LangGraph supports multiple streaming modes:

- **Event Streaming**: Receive updates as each node completes
- **Token Streaming**: Real-time LLM token output
- **Custom Streaming**: User-defined stream events

**Current State:**
```python
# _runner.py line 128
loop = AsyncPregelLoop(
    ...
    stream=None,  # Hardcoded - no streaming support
    ...
)
```

**Temporal Mapping:**
```python
# Option 1: Workflow Updates (recommended for real-time)
@workflow.defn
class LangGraphWorkflow:
    @workflow.update
    async def get_stream_event(self) -> StreamEvent:
        # Return latest stream event
        return self._latest_event

# Option 2: Workflow Queries (for polling)
@workflow.defn
class LangGraphWorkflow:
    @workflow.query
    def get_execution_state(self) -> dict:
        return {
            "current_node": self._current_node,
            "completed_nodes": self._completed_nodes,
            "partial_output": self._partial_output,
        }
```

**Implementation Complexity:** Medium
**Priority:** Out of Scope - Streaming is not planned for this integration

> **Note:** Streaming is explicitly out of scope for the LangGraph-Temporal integration.
> The primary value of this integration is durable execution of LangGraph workflows
> with Temporal's reliability guarantees (retries, timeouts, persistence). Streaming
> is a real-time concern that doesn't align well with Temporal's activity-based
> execution model where nodes run as discrete, durable units. Users requiring
> streaming should use LangGraph directly for those use cases.

---

### 3. Checkpointing / State Persistence

**LangGraph Feature:**
Checkpointing enables:

- **Thread-based Conversations**: Continue conversations using `thread_id`
- **Failure Recovery**: Resume from last successful state
- **Time Travel**: Replay from any checkpoint
- **Cross-session Memory**: Maintain state across workflow executions

**Current State:**
Option 3 (Continue-As-New with checkpoint) is now implemented:
- `get_state()` method returns a `StateSnapshot` with current execution state
- `compile(checkpoint=...)` parameter restores from a previous checkpoint
- Workflow can checkpoint and continue-as-new to manage history size

```python
# Continue-As-New with checkpoint - IMPLEMENTED
@workflow.defn
class LongRunningAgentWorkflow:
    @workflow.run
    async def run(self, input_data: dict, checkpoint: dict | None = None):
        app = compile("my_graph", checkpoint=checkpoint)
        result = await app.ainvoke(input_data)

        # Check if we should continue-as-new (e.g., history too long)
        if workflow.info().get_current_history_length() > 10000:
            snapshot = app.get_state()
            workflow.continue_as_new(input_data, snapshot.model_dump())

        return result
```

**Other Options (Not Implemented):**
```python
# Option 1: Workflow state as checkpoint (manual tracking)
# Option 2: External checkpoint store (for very large state)
```

**Implementation Complexity:** Medium (Option 3 implemented)
**Priority:** ✅ Partial - Continue-as-new pattern available

---

### 4. Store (Cross-Thread Persistence)

**LangGraph Feature:**
The `Store` API provides persistent memory shared across threads:

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
graph = builder.compile(store=store)

# In nodes, access store via config
def my_node(state, config):
    store = config["configurable"]["store"]
    # Read/write cross-thread data
    memories = store.search(("user", user_id), query="preferences")
```

**Current State:** ✅ Implemented

The implementation uses `ActivityLocalStore` which:
- Receives a snapshot of store data before each activity execution
- Tracks writes made during node execution
- Returns writes back to the workflow which applies them to its state
- Persists store data across nodes and invocations within a workflow

```python
# Usage - store is automatically available via get_store()
def my_node(state):
    from langgraph.config import get_store
    store = get_store()
    item = store.get(("user", user_id), "preferences")
    store.put(("user", user_id), "preferences", {"theme": "dark"})
    return state
```

**Implementation Complexity:** Medium-High
**Priority:** ✅ Implemented

---

## 🟡 Important Missing Features

### 5. Send API / Dynamic Parallelism

**LangGraph Feature:**
The `Send` API enables map-reduce patterns with dynamic parallelism:

```python
from langgraph.types import Send

def route_to_workers(state):
    # Dynamically create N parallel tasks
    return [
        Send("worker_node", {"task": task})
        for task in state["tasks"]
    ]

graph.add_conditional_edges("dispatcher", route_to_workers)
```

**Current State:** ✅ Implemented and Validated

The implementation:
- Captures `Send` objects via `CONFIG_KEY_SEND` in activities
- Converts them to `SendPacket` for serialization
- Executes each `SendPacket` as a separate activity with `Send.arg` as input
- Accumulates results using state reducers (e.g., `operator.add`)

```python
# Test case: test_validation.py::test_send_api_dynamic_parallelism
def continue_to_workers(state):
    return [Send("worker", {"item": item}) for item in state["items"]]

def worker_node(state):
    return {"results": [state["item"] * 2]}  # Each worker gets Send.arg as input
```

**Implementation Complexity:** Medium
**Priority:** ✅ Implemented

---

### 6. Command API

**LangGraph Feature:**
The `Command` object combines state updates with navigation:

```python
from langgraph.types import Command

def my_node(state):
    # Update state AND navigate to specific node
    return Command(
        goto="next_node",
        update={"processed": True},
    )

# For subgraphs - navigate to parent
def subgraph_node(state):
    return Command(
        goto="parent_handler",
        graph=Command.PARENT,
        update={"result": state["result"]},
    )
```

**Current State:** ✅ Implemented and Validated

The implementation works through native Pregel loop support. When a node returns a `Command`:
- The Pregel loop handles `Command(goto=...)` routing
- State updates from `Command(update=...)` are applied to channels
- No special handling needed in the Temporal runner

**Note:** When using `Command(goto=...)`, do NOT add a static edge from the node.
The `Command` determines routing - if you have both static edge and Command,
both paths will execute.

```python
# Test case: test_validation.py::test_command_goto_skip_node
def start_node(state):
    if state["value"] > 10:
        return Command(goto="finish", update={"path": ["start"]})
    else:
        return Command(goto="middle", update={"path": ["start"]})
```

**Implementation Complexity:** Medium
**Priority:** ✅ Implemented

---

### 7. Subgraphs / Nested Graphs

**LangGraph Feature:**
Hierarchical graph composition:

```python
# Define subgraph
subgraph = StateGraph(SubState)
subgraph.add_node("sub_node", sub_node_fn)
sub_compiled = subgraph.compile()

# Add as node in parent
parent = StateGraph(ParentState)
parent.add_node("subgraph", sub_compiled)
```

**Current State:** ✅ Implemented and Validated

Subgraphs work through native Pregel loop support:
- Compiled subgraphs can be added as nodes in parent graphs
- State flows correctly between parent and child graphs
- Child node execution happens as activities like regular nodes

```python
# Test case: test_validation.py::test_subgraph_execution
child = StateGraph(ChildState)
child.add_node("child_process", lambda s: {"child_result": s["value"] * 3})
child_compiled = child.compile()

parent = StateGraph(ParentState)
parent.add_node("parent_start", parent_start_fn)
parent.add_node("child_graph", child_compiled)  # Compiled graph as node
parent.add_edge("parent_start", "child_graph")
```

**Implementation Complexity:** Medium
**Priority:** ✅ Implemented

---

### 8. Query/Signal Handlers

**Temporal Feature:**
Native Temporal capability for workflow interaction:

```python
@workflow.defn
class LangGraphWorkflow:
    @workflow.query
    def get_current_state(self) -> dict:
        """Expose current graph state."""
        return self._current_state

    @workflow.query
    def get_execution_progress(self) -> dict:
        """Return execution progress."""
        return {
            "completed_nodes": self._completed_nodes,
            "current_node": self._current_node,
            "total_nodes": self._total_nodes,
        }

    @workflow.signal
    def cancel_execution(self):
        """Signal to cancel graph execution."""
        self._should_cancel = True

    @workflow.signal
    def update_config(self, config: dict):
        """Update configuration mid-execution."""
        self._runtime_config.update(config)
```

**Current State:** Not implemented

**Implementation Complexity:** Low-Medium
**Priority:** Important - Enables observability and control

---

### 9. Continue-As-New

**Temporal Feature:**
For long-running agents that may exceed history limits:

```python
@workflow.defn
class LangGraphWorkflow:
    @workflow.run
    async def run(self, input_state: dict, checkpoint: Optional[dict] = None):
        # Restore from checkpoint if provided
        if checkpoint:
            self._restore_checkpoint(checkpoint)

        # Execute graph...

        # If approaching history limit, continue-as-new
        if workflow.info().get_current_history_length() > 10000:
            checkpoint = self._create_checkpoint()
            workflow.continue_as_new(input_state, checkpoint)
```

**Current State:** Not implemented

**Implementation Complexity:** Medium
**Priority:** Important - Required for very long-running agents

---

## 🟢 Nice-to-Have Features

### 10. Observability/Tracing

**Current State:** Basic heartbeats in activities

**Improvements:**
- OpenTelemetry spans per node
- LangSmith integration
- Structured logging with node context
- Temporal's built-in tracing support

**Implementation Complexity:** Low-Medium
**Priority:** Nice-to-have

---

### 11. Static Breakpoints

**LangGraph Feature:**
Pause before/after specific nodes (predefined, not dynamic):

```python
graph = builder.compile(
    interrupt_before=["human_review"],
    interrupt_after=["tool_execution"],
)
```

**Temporal Mapping:**
```python
# Could use per-node metadata
graph.add_node(
    "human_review",
    review_fn,
    metadata={
        "temporal": {
            "interrupt_before": True,
        }
    }
)
```

**Implementation Complexity:** Medium
**Priority:** Nice-to-have (dynamic interrupts are more flexible)

---

### 12. Error State Recovery

**Feature:**
Resume from last successful node after unrecoverable error.

**Current State:** Temporal retries handle transient failures, but no graph-level checkpoint/resume.

**Implementation Complexity:** Medium-High
**Priority:** Nice-to-have

---

### 13. Cache

**LangGraph Feature:**
Caching to avoid redundant LLM calls:

```python
from langgraph.cache import InMemoryCache

cache = InMemoryCache()
graph = builder.compile(cache=cache)
```

**Current State:**
```python
# _runner.py line 132
cache=getattr(self.pregel, "cache", None),  # Passed through but not Temporal-aware
```

**Implementation Complexity:** Medium
**Priority:** Nice-to-have

---

## Summary Table

| Feature | Priority | Status | Complexity | Temporal Mapping |
|---------|----------|--------|------------|------------------|
| Human-in-the-Loop | ✅ Implemented | Complete | High | Signals + wait_condition |
| Streaming | ⚪ Out of Scope | N/A | Medium | N/A |
| Checkpointing | 🟡 Partial | CAN pattern | Medium | get_state() + compile(checkpoint=) |
| Store | ✅ Implemented | Complete | Medium-High | ActivityLocalStore + workflow state |
| Send API | ✅ Implemented | Complete | Medium | SendPacket serialization + activity |
| Command API | ✅ Implemented | Complete | Medium | Native Pregel support |
| Subgraphs | ✅ Implemented | Complete | Medium | Native Pregel support |
| Query/Signal | 🟡 Important | Missing | Low-Medium | workflow.signal/query |
| Continue-As-New | 🟡 Important | Missing | Medium | workflow.continue_as_new |
| Observability | 🟢 Nice-to-have | Partial | Low-Medium | OpenTelemetry |
| Breakpoints | 🟢 Nice-to-have | Missing | Medium | Metadata flag |
| Error Recovery | 🟢 Nice-to-have | Partial | Medium-High | Checkpoints |
| Cache | 🟢 Nice-to-have | Partial | Medium | Temporal-aware cache |

---

## Recommended Implementation Order

### ✅ Phase 5: Human-in-the-Loop (Complete)
1. ✅ Implement `interrupt()` support using Temporal signals
2. ✅ Add `@workflow.signal` handler for interrupt responses
3. ✅ Implement `workflow.wait_condition()` for blocking on interrupts
4. ✅ Support interrupt data serialization

### ✅ Phase 6: Store (Complete)
1. ✅ Implement `ActivityLocalStore` for node-level store access
2. ✅ Add `StoreSnapshot` for passing store data to activities
3. ✅ Track store writes and apply to workflow state
4. ✅ Persist store across nodes and invocations

### ✅ Phase 7: Advanced Patterns (Complete)
1. ✅ Validate and implement Send API support
2. ✅ Validate subgraph support
3. ✅ Validate Command API handling

### Phase 8: Observability (Future)
1. Implement `@workflow.query` for execution state
2. Add OpenTelemetry tracing per node
3. Add structured logging with node context

### Phase 9: Enhanced Checkpointing (Future)
1. Implement thread_id support for multi-turn conversations
2. Create external checkpoint store for very large state
3. Support conversation continuation across workflow executions

---

## References

- [LangGraph Interrupts Documentation](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Human-in-the-Loop Guide](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/wait-user-input/)
- [Map-Reduce with Send API](https://langchain-ai.github.io/langgraphjs/how-tos/map-reduce/)
- [Subgraphs Guide](https://langchain-ai.github.io/langgraphjs/how-tos/subgraph/)
- [LangGraph Functional API Blog](https://blog.langchain.com/introducing-the-langgraph-functional-api/)

---

**End of Document**
