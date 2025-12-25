# LangGraph-Temporal Integration: Phase 1 Validation Summary

## Overview

This document summarizes the findings from Phase 1 prototype validation of the
LangGraph-Temporal integration proposal. All technical concerns have been
validated with working prototypes and tests.

**Validation Status: PASSED**

All 80 tests pass, confirming the feasibility of the proposed approach.

---

## Technical Concerns Validated

### 1. AsyncPregelLoop API - Submit Function Injection

**Question:** Can we inject a custom submit function to intercept parallel node execution?

**Answer:** Yes

**Findings:**
- `CONFIG_KEY_RUNNER_SUBMIT` config key allows injecting a custom submit function
- Import from `langgraph._internal._constants` to avoid deprecation warning
- The submit function receives `PregelExecutableTask` objects for parallel nodes
- Sequential graphs may use a fast path that bypasses the submit function
- The executor must have a `loop` attribute pointing to the event loop

**Key Code:**
```python
from langgraph._internal._constants import CONFIG_KEY_RUNNER_SUBMIT
from weakref import WeakMethod

config: RunnableConfig = {
    "configurable": {
        CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
    }
}
```

**Files:**
- `pregel_loop_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_pregel_loop.py` - 7 tests

---

### 2. Write Capture - Output Collection

**Question:** Does the CONFIG_KEY_SEND callback work for capturing node outputs?

**Answer:** Yes, but using task.writes is simpler

**Findings:**
- `PregelExecutableTask.writes` is a `deque[tuple[str, Any]]` that captures outputs
- Writes are populated after task execution completes
- Each write is a (channel_name, value) tuple
- Parallel nodes maintain separate writes deques

**Key Code:**
```python
# After executing task.proc:
for channel, value in task.writes:
    # channel is the output channel name
    # value is the node's output
    pass
```

**Files:**
- `write_capture_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_write_capture.py` - 4 tests

---

### 3. Task Interface - PregelExecutableTask Structure

**Question:** What is the actual PregelExecutableTask structure?

**Answer:** Frozen dataclass with well-defined fields

**Findings:**
- `PregelExecutableTask` is a frozen (immutable) dataclass
- Key fields for Temporal activities:
  - `name`: Node name (string)
  - `id`: Unique task ID (string)
  - `path`: Graph hierarchy path (tuple)
  - `input`: Input state to the node
  - `proc`: The node's runnable (not serialized)
  - `config`: RunnableConfig (needs filtering)
  - `triggers`: Channels that triggered this task
  - `writes`: Output writes deque (not serialized)
  - `retry_policy`: LangGraph retry config (can map to Temporal)
  - `cache_key`: Cache key (can use Temporal memoization)

**Field Categories for Temporal:**
| Category | Fields |
|----------|--------|
| Pass to activity | name, id, input, path, triggers |
| Filter for serialization | config |
| Reconstruct in activity | proc, writers, subgraphs |
| Activity output | writes |
| Policy mapping | retry_policy, cache_key |

**Key Code:**
```python
def filter_config_for_serialization(config: RunnableConfig) -> dict[str, Any]:
    """Filter out internal keys like __pregel_* and __lg_*"""
    # See task_interface_proto.py for full implementation
```

**Files:**
- `task_interface_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_task_interface.py` - 11 tests

---

### 4. Serialization - State and Messages

**Question:** Can LangGraph state be serialized for Temporal activities?

**Answer:** Yes, using Temporal's pydantic_data_converter with ChannelWrite for message types

**Findings:**
- LangChain messages (HumanMessage, AIMessage, etc.) are Pydantic v2 models
- Temporal's `pydantic_data_converter` handles them automatically when typed explicitly
- Basic TypedDict states work with default JSON converter
- **CRITICAL:** `Any` typed fields lose Pydantic model type information during serialization
- LangChain messages in `Any` fields become plain dicts and require explicit reconstruction
- Use `ChannelWrite` model with `value_type` field to preserve message types
- End-to-end workflow/activity tests confirm round-trip serialization works

**Key Code:**
```python
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

client = await Client.connect(
    "localhost:7233",
    data_converter=pydantic_data_converter,
)
```

**Sandbox Configuration:**
When using LangChain types in workflows, configure sandbox passthrough:
```python
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

sandbox_runner = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "langchain_core",
        "langchain_core.messages",
        "langchain_core.messages.human",
        "langchain_core.messages.ai",
    )
)
```

**NodeActivity Input/Output Models:**
```python
class NodeActivityInput(BaseModel):
    """Single Pydantic model for all activity input data."""
    node_name: str           # Node to execute
    task_id: str             # Unique task ID
    graph_builder_path: str  # Module path to graph builder
    input_state: dict[str, Any]  # State to pass to node
    config: dict[str, Any]   # Filtered RunnableConfig
    path: tuple[str | int, ...]  # Graph hierarchy path
    triggers: list[str]      # Triggering channels

class ChannelWrite(BaseModel):
    """Preserves type info for values that may contain Pydantic models."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    channel: str
    value: Any
    value_type: str | None = None  # "message" or "message_list"

    @classmethod
    def create(cls, channel: str, value: Any) -> "ChannelWrite":
        """Factory that records value_type for LangChain messages."""
        value_type = None
        if isinstance(value, BaseMessage):
            value_type = "message"
        elif isinstance(value, list) and value and isinstance(value[0], BaseMessage):
            value_type = "message_list"
        return cls(channel=channel, value=value, value_type=value_type)

    def reconstruct_value(self) -> Any:
        """Reconstruct LangChain messages from serialized dicts."""
        if self.value_type == "message" and isinstance(self.value, dict):
            return reconstruct_message(self.value)  # Uses message's "type" field
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [reconstruct_message(item) if isinstance(item, dict) else item
                    for item in self.value]
        return self.value

class NodeActivityOutput(BaseModel):
    """Single Pydantic model for activity output."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    writes: list[ChannelWrite]

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert to (channel, value) tuples with reconstructed messages."""
        return [(w.channel, w.reconstruct_value()) for w in self.writes]
```

**Files:**
- `serialization_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_serialization.py` - 26 tests (including end-to-end)

---

### 5. Graph Builder - Node Reconstruction

**Question:** How do activities get access to node functions to execute them?

**Answer:** Rebuild graph from builder function (recommended)

**Options Explored:**

| Option | Approach | Pros | Cons |
|--------|----------|------|------|
| 1 | Import by module path | Simple, standard Python | Functions must be importable |
| 2 | Function registry | Flexible, supports lambdas | Global state, registration needed |
| 3 | Rebuild graph (recommended) | Most robust, consistent | Slight overhead |

**Recommended Approach:**
```python
def get_node_from_graph_builder(builder_path: str, node_name: str) -> Any:
    """Get a node function by rebuilding the graph."""
    # Import the builder function
    builder_func = import_function(builder_path)

    # Build the graph
    compiled_graph = builder_func()

    # Get the node
    return compiled_graph.nodes[node_name]
```

**User Pattern:**
```python
# In myapp/agents.py:
def build_agent_graph():
    graph = StateGraph(AgentState)
    graph.add_node("fetch", fetch_data)
    graph.add_node("process", process_data)
    # ...
    return graph.compile()

# Activity receives builder_path="myapp.agents.build_agent_graph"
# and node_name="fetch" to execute the node
```

**Files:**
- `graph_builder_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_graph_builder.py` - 19 tests

---

### 6. Graph Registry - Thread-Safe Caching (V3.1)

**Question:** Can we cache compiled graphs per worker process and look up nodes safely from multiple threads? Do lambdas work?

**Answer:** Yes - Thread-safe caching works, lambdas are preserved

**Findings:**
- Graphs can be cached per worker process using thread-safe locking
- Double-checked locking pattern ensures graph is built exactly once
- Lambdas and closures work correctly - they're captured in the compiled graph
- Class methods with instance state also preserved
- `PregelNode.invoke()` works correctly for direct node invocation
- Concurrent access from 20+ threads with 1000+ operations confirmed safe

**Thread-Safe Registry Pattern:**
```python
class GraphRegistry:
    def __init__(self) -> None:
        self._builders: dict[str, Callable[[], Pregel]] = {}
        self._cache: dict[str, Pregel] = {}
        self._lock = threading.Lock()

    def get_graph(self, graph_id: str) -> Pregel:
        # Fast path: check cache without lock
        if graph_id in self._cache:
            return self._cache[graph_id]

        # Slow path: acquire lock and build if needed
        with self._lock:
            # Double-check after acquiring lock
            if graph_id in self._cache:
                return self._cache[graph_id]

            builder = self._builders[graph_id]
            graph = builder()
            self._cache[graph_id] = graph
            return graph
```

**Lambda Support (V3.1 Key Finding):**
```python
def build_graph():
    multiplier = 10  # Closure variable

    graph = StateGraph(dict)
    # Lambda works! Captured in compiled graph, cached per worker
    graph.add_node("multiply", lambda state: {
        "value": state["value"] * multiplier
    })
    return graph.compile()

# Plugin caches the compiled graph
plugin = LangGraphPlugin(graphs={"my_graph": build_graph})
```

**Validation Results:**
| Test | Result | Details |
|------|--------|---------|
| Basic Registry | ✅ | Same instance returned, built once |
| Lambda Preservation | ✅ | Closures work: `3 * 10 + 5 = 35` |
| Node Lookup | ✅ | `PregelNode` accessible by name |
| Class Methods | ✅ | Instance state preserved |
| Concurrent Same Graph | ✅ | 20 threads × 50 iterations, built once |
| Concurrent Different Graphs | ✅ | 3 graphs, all built exactly once |
| Concurrent Node Invocation | ✅ | 200 direct node invocations, all correct |

**Files:**
- `graph_registry_proto.py` - Prototype implementation
- `tests/contrib/langgraph/prototypes/test_graph_registry.py` - 12 tests

---

## Test Summary

| Test File | Tests | Status |
|-----------|-------|--------|
| test_pregel_loop.py | 8 | PASSED |
| test_write_capture.py | 4 | PASSED |
| test_task_interface.py | 11 | PASSED |
| test_serialization.py | 26 | PASSED |
| test_graph_builder.py | 19 | PASSED |
| test_graph_registry.py | 12 | PASSED |
| **Total** | **80** | **PASSED** |

---

## Conclusions

All technical concerns for the LangGraph-Temporal integration have been validated:

1. **Submit injection works** - We can intercept parallel node execution
2. **Write capture works** - Node outputs are captured in task.writes
3. **Task interface is stable** - PregelExecutableTask is a well-defined dataclass
4. **Serialization works** - Use pydantic_data_converter for LangChain messages + ChannelWrite for type preservation
5. **Graph rebuild works** - Activities can reconstruct graphs from builder functions
6. **Thread-safe caching works** - Graphs can be cached per worker with concurrent access (V3.1)

**Critical Discoveries:**
- LangChain messages in `Any` typed fields lose type information during serialization.
  The `ChannelWrite` pattern with `value_type` field preserves message types.
- **Lambdas work with caching!** Since graphs are cached per worker process, lambda references
  are preserved. No need to restrict users to named functions only.

The proposed architecture is feasible and can proceed to Phase 2 implementation.

---

## Next Steps (Phase 2)

1. Implement `TemporalPregelLoop` class
2. Create activity wrapper for node execution
3. Build workflow orchestrator
4. Add checkpointer integration
5. Write integration tests

---

## API Stability Notes

- `CONFIG_KEY_RUNNER_SUBMIT` - Internal API, import from `langgraph._internal._constants`
- `PregelExecutableTask` - Public type from `langgraph.types`
- `compiled_graph.nodes` - Public API for accessing nodes
