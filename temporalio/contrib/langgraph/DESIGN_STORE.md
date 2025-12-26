# LangGraph Store Integration Design

**Date:** 2025-01-25
**Status:** Proposal
**Author:** Claude

---

## Overview

This document proposes a design for integrating LangGraph's Store API with Temporal workflows. The Store provides cross-thread persistent memory that nodes can read/write during execution.

## LangGraph Store API

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
graph = builder.compile(store=store)

# In nodes, access store via config
def my_node(state, config):
    store = config["configurable"]["store"]

    # Namespaced key-value operations
    store.put(("user", user_id), "preferences", {"theme": "dark"})
    items = store.search(("user", user_id), query="preferences")
    store.delete(("user", user_id), "old_key")

    return state
```

### Store Operations

| Operation | Description |
|-----------|-------------|
| `put(namespace, key, value)` | Write a value |
| `get(namespace, key)` | Read a single value |
| `search(namespace, query)` | Search within namespace |
| `delete(namespace, key)` | Delete a value |

### Safety Guarantees (Native LangGraph)

1. **Durability**: Depends on backend (InMemory = none, PostgresStore = durable)
2. **Consistency**: Read-your-writes within same thread
3. **Isolation**: No transactions - concurrent writes may interleave
4. **No rollback**: Failed nodes don't rollback store writes

---

## Problem Statement

In our Temporal integration:
- Nodes execute as **activities** (separate process/context)
- Activities cannot directly access workflow memory
- InMemoryStore in workflow is invisible to activities
- Store writes in activities are lost on worker restart

---

## Proposed Design

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Temporal Workflow                                              │
│                                                                 │
│  _store_state: dict[tuple, dict[str, Any]]  ← Canonical state   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  ainvoke()                                               │   │
│  │                                                          │   │
│  │  1. Serialize relevant store slice → activity input      │   │
│  │  2. Execute activity                                     │   │
│  │  3. Receive store writes from activity output            │   │
│  │  4. Apply writes to _store_state                         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                           │                                     │
│                           ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Activity                                                │   │
│  │                                                          │   │
│  │  ActivityLocalStore (captures reads/writes)              │   │
│  │       │                                                  │   │
│  │       ▼                                                  │   │
│  │  Node executes, calls store.put/get/search               │   │
│  │       │                                                  │   │
│  │       ▼                                                  │   │
│  │  Return writes: [(namespace, key, value), ...]           │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Data Models

```python
from pydantic import BaseModel
from typing import Any

class StoreItem(BaseModel):
    """Single item in the store."""
    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]

class StoreWrite(BaseModel):
    """A write operation to be applied."""
    operation: Literal["put", "delete"]
    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any] | None = None  # None for delete

class StoreSnapshot(BaseModel):
    """Subset of store data passed to activity."""
    items: list[StoreItem]

# Updated activity models
class NodeActivityInput(BaseModel):
    # ... existing fields ...
    store_snapshot: StoreSnapshot | None = None

class NodeActivityOutput(BaseModel):
    # ... existing fields ...
    store_writes: list[StoreWrite] = []
```

### ActivityLocalStore

A store implementation that captures operations for later replay in workflow:

```python
from langgraph.store.base import BaseStore

class ActivityLocalStore(BaseStore):
    """Store that captures writes and serves reads from snapshot."""

    def __init__(self, snapshot: StoreSnapshot):
        self._snapshot = {
            (tuple(item.namespace), item.key): item.value
            for item in snapshot.items
        }
        self._writes: list[StoreWrite] = []
        self._local_cache: dict[tuple, dict[str, Any]] = {}

    def put(self, namespace: tuple[str, ...], key: str, value: dict) -> None:
        # Record write for workflow
        self._writes.append(StoreWrite(
            operation="put",
            namespace=namespace,
            key=key,
            value=value,
        ))
        # Update local cache for read-your-writes
        self._local_cache[(namespace, key)] = value

    def get(self, namespace: tuple[str, ...], key: str) -> dict | None:
        # Check local writes first (read-your-writes)
        if (namespace, key) in self._local_cache:
            return self._local_cache[(namespace, key)]
        # Fall back to snapshot
        return self._snapshot.get((namespace, key))

    def search(self, namespace: tuple[str, ...], query: str = "") -> list[dict]:
        # Search in snapshot + local writes
        results = []
        for (ns, key), value in {**self._snapshot, **self._local_cache}.items():
            if ns == namespace:
                results.append({"key": key, "value": value})
        return results

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        self._writes.append(StoreWrite(
            operation="delete",
            namespace=namespace,
            key=key,
        ))
        self._local_cache.pop((namespace, key), None)

    def get_writes(self) -> list[StoreWrite]:
        return self._writes
```

### Runner Changes

```python
class TemporalLangGraphRunner:
    def __init__(self, ...):
        # ... existing fields ...
        self._store_state: dict[tuple[tuple[str, ...], str], dict] = {}

    async def _execute_as_activity(
        self,
        task: PregelExecutableTask,
        resume_value: Optional[Any] = None,
    ) -> list[tuple[str, Any]]:
        # Prepare store snapshot for this node
        store_snapshot = self._prepare_store_snapshot(task)

        activity_input = NodeActivityInput(
            # ... existing fields ...
            store_snapshot=store_snapshot,
        )

        result = await workflow.execute_activity(...)

        # Apply store writes to workflow state
        self._apply_store_writes(result.store_writes)

        return result.to_write_tuples()

    def _prepare_store_snapshot(self, task) -> StoreSnapshot | None:
        """Prepare store data needed by this node."""
        if not self._store_state:
            return None

        # Option 1: Send entire store (simple, but may be large)
        # Option 2: Send only namespaces the node will access (requires hints)
        items = [
            StoreItem(namespace=list(ns), key=key, value=value)
            for (ns, key), value in self._store_state.items()
        ]
        return StoreSnapshot(items=items)

    def _apply_store_writes(self, writes: list[StoreWrite]) -> None:
        """Apply store writes from activity to workflow state."""
        for write in writes:
            key = (tuple(write.namespace), write.key)
            if write.operation == "put":
                self._store_state[key] = write.value
            elif write.operation == "delete":
                self._store_state.pop(key, None)
```

### Activity Changes

```python
async def execute_node(input: NodeActivityInput) -> NodeActivityOutput:
    # Create activity-local store from snapshot
    store = None
    if input.store_snapshot:
        store = ActivityLocalStore(input.store_snapshot)

    # Inject store into config
    config = {
        **input.config,
        "configurable": {
            **input.config.get("configurable", {}),
            CONFIG_KEY_STORE: store,  # LangGraph's store config key
        },
    }

    # Execute node
    # ... existing execution code ...

    # Collect store writes
    store_writes = store.get_writes() if store else []

    return NodeActivityOutput(
        writes=writes,
        interrupt=interrupt,
        store_writes=store_writes,
    )
```

---

## Safety Guarantees

### What We Guarantee

1. **Durability within workflow**: Store state is part of workflow state, survives replays
2. **Read-your-writes**: Within a node, reads see previous writes from same node
3. **Sequential consistency**: Nodes in sequence see each other's writes
4. **Continue-as-new support**: Store state included in checkpoint

### What We Don't Guarantee

1. **Cross-workflow consistency**: Different workflow executions don't share store
2. **Parallel node isolation**: Parallel nodes may have stale reads
3. **Atomic multi-key operations**: No transactions
4. **Rollback on failure**: Activity failures don't rollback writes (activity didn't complete)

### Parallel Node Handling

When nodes execute in parallel, each receives a snapshot from before the tick:

```
Tick N:
  _store_state = {("user", "123"): {"count": 0}}

  ┌─────────────────┐     ┌─────────────────┐
  │ Node A          │     │ Node B          │
  │ snapshot: {0}   │     │ snapshot: {0}   │
  │ writes: {+1}    │     │ writes: {+2}    │
  └────────┬────────┘     └────────┬────────┘
           │                       │
           ▼                       ▼
  Apply writes in order (A then B, or deterministic order)

  Final: _store_state = {("user", "123"): {"count": 2}}
  (Last write wins - same as LangGraph's native behavior)
```

---

## Checkpoint Integration

Store state is included in StateSnapshot:

```python
class StateSnapshot(BaseModel):
    values: dict[str, Any]
    next: tuple[str, ...]
    metadata: dict[str, Any]
    tasks: tuple[dict[str, Any], ...]
    store_state: dict[str, Any] = {}  # NEW: serialized store

def get_state(self) -> StateSnapshot:
    return StateSnapshot(
        # ... existing fields ...
        store_state=self._serialize_store_state(),
    )

def _restore_from_checkpoint(self, checkpoint: dict) -> None:
    # ... existing restoration ...
    self._store_state = self._deserialize_store_state(
        checkpoint.get("store_state", {})
    )
```

---

## External Store Option

For true cross-workflow persistence, users can provide an external store:

```python
# User provides their own store implementation
class RedisStore(BaseStore):
    def __init__(self, redis_client):
        self._redis = redis_client

    async def put(self, namespace, key, value):
        await self._redis.hset(f"{namespace}", key, json.dumps(value))

    # ... etc

# Usage
plugin = LangGraphPlugin(
    graphs={"my_graph": build_graph},
    store=RedisStore(redis_client),  # Shared across all workflows
)
```

This bypasses the snapshot mechanism - activities access Redis directly.

---

## API Changes

### compile() function

```python
def compile(
    graph_id: str,
    *,
    # ... existing params ...
    store: Optional[BaseStore] = None,  # NEW: external store
) -> TemporalLangGraphRunner:
```

### LangGraphPlugin

```python
class LangGraphPlugin:
    def __init__(
        self,
        graphs: dict[str, Callable[[], Pregel]],
        *,
        # ... existing params ...
        store: Optional[BaseStore] = None,  # NEW: shared store
    ):
```

---

## Implementation Plan

### Phase 1: Basic Store Support
1. Add StoreWrite, StoreSnapshot models
2. Implement ActivityLocalStore
3. Update activity input/output
4. Add _store_state to runner
5. Wire up snapshot passing and write application

### Phase 2: Checkpoint Integration
1. Add store_state to StateSnapshot
2. Serialize/deserialize store state
3. Test with continue-as-new

### Phase 3: External Store Support
1. Add store parameter to compile/plugin
2. Detect external store and bypass snapshot mechanism
3. Document external store requirements

---

## Open Questions

1. **Store size limits**: Should we limit snapshot size? Warn on large stores?
2. **Namespace hints**: Should nodes declare which namespaces they access?
3. **Conflict resolution**: For parallel writes, use last-write-wins or merge?
4. **Search implementation**: How to handle search queries efficiently?

---

## References

- [LangGraph Store Documentation](https://langchain-ai.github.io/langgraph/concepts/persistence/#memory-store)
- [BaseStore Interface](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/store/base.py)
