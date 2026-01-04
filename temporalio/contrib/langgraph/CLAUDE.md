# LangGraph-Temporal Integration: Design Guide for Claude

This document captures design decisions and non-obvious implementation details to help future Claude sessions understand and modify this integration.

## Architecture Overview

This integration enables LangGraph graphs to run as Temporal workflows, providing durable execution for AI agents. There are **two distinct execution models** for LangGraph's two API styles:

| API Style | Execution Model | Runner Class |
|-----------|-----------------|--------------|
| **Graph API** (`StateGraph`) | BSP model via `AsyncPregelLoop` | `TemporalLangGraphRunner` |
| **Functional API** (`@entrypoint/@task`) | Direct function execution with injected callback | `TemporalFunctionalRunner` |

## Key Design Decisions

### 1. Why Two Runners Instead of One?

**Graph API** uses LangGraph's `AsyncPregelLoop` for orchestration. The loop handles:
- Step-by-step execution with `tick()`/`after_tick()`
- Conditional edge evaluation
- Channel/state management
- BSP (Bulk Synchronous Parallel) task scheduling

**Functional API** cannot use `AsyncPregelLoop` because Pregel's runner **always overwrites** `CONFIG_KEY_CALL` with its own implementation. To route `@task` calls to Temporal activities, we must:
1. Extract the entrypoint function from the Pregel wrapper
2. Execute it directly (bypassing Pregel's loop)
3. Inject our own `CONFIG_KEY_CALL` callback via context variable

### 2. Entrypoint vs Graph Detection

Both `@entrypoint` and `StateGraph.compile()` return `Pregel` objects, but they differ:

```python
# @entrypoint creates:
# - Plain Pregel (NOT CompiledStateGraph subclass)
# - Single node named after the function
# - NO __start__ node

# StateGraph.compile() creates:
# - CompiledStateGraph (subclass of Pregel)
# - Multiple nodes including __start__
# - User-defined nodes connected via edges
```

The `_is_entrypoint()` function in `_plugin.py` distinguishes these by checking:
1. `isinstance(value, CompiledStateGraph)` → False for entrypoints
2. `"__start__" in value.nodes` → False for entrypoints
3. `len(value.nodes) == 1` → True for entrypoints

### 3. BSP (Bulk Synchronous Parallel) Execution Model

LangGraph uses BSP where all ready tasks in a step execute concurrently. In `_runner.py`:

```python
async def _execute_loop_tasks(self, tasks, loop):
    # Execute ALL tasks in parallel - this is critical for correctness
    results = await asyncio.gather(
        *[self._execute_task(task, loop) for task in tasks]
    )
    # If ANY task interrupts, the entire step is interrupted
    return not all(results)
```

**Important**: Never execute tasks sequentially - it breaks LangGraph's execution semantics and can cause incorrect behavior with conditional edges.

### 4. LangGraph Internal APIs Used

The Functional API runner uses several private LangGraph APIs:

| API | Location | Purpose |
|-----|----------|---------|
| `CONFIG_KEY_CALL` | `langgraph._internal._constants` | Callback for routing @task calls |
| `CONFIG_KEY_SCRATCHPAD` | `langgraph._internal._constants` | For `interrupt()` support |
| `CONFIG_KEY_RUNTIME` | `langgraph._internal._constants` | For `get_store()`/`get_stream_writer()` |
| `PregelScratchpad` | `langgraph._internal._scratchpad` | Tracks interrupt state |
| `var_child_runnable_config` | `langchain_core.runnables.config` | Context var for config injection |

**Risk**: These are private APIs that may change. The integration may need updates when LangGraph releases new versions.

### 5. Message Serialization Gotcha

LangChain messages in `Any`-typed fields **lose type information** during Temporal serialization:

```python
# Before serialization
writes = [("messages", AIMessage(content="Hello"))]

# After round-trip through Temporal
writes = [("messages", {"content": "Hello", "type": "ai", ...})]  # Dict!
```

**Solution**: The `ChannelWrite` model in `_models.py` includes a `value_type` field that enables reconstruction:

```python
@dataclass
class ChannelWrite:
    channel: str
    value: Any
    value_type: str | None = None  # "message" or "message_list"

    def reconstruct_value(self) -> Any:
        if self.value_type == "message" and isinstance(self.value, dict):
            return _coerce_to_message(self.value)
        # ...
```

### 6. Why Nodes Run as Activities (Not in Workflow)

Nodes execute as activities because:
1. **LLM calls are non-deterministic** - same prompt can return different responses
2. **Tool execution has side effects** - API calls, database writes
3. **Activities provide retries** - transient failures are automatically retried
4. **Timeouts are configurable** - per-node timeout control

Exception: `run_in_workflow=True` metadata allows deterministic nodes (pure transforms, routing logic) to run directly in the workflow for efficiency.

### 7. Config Filtering

The `_filter_config()` method removes non-serializable values before passing to activities:

- `callbacks` - LangChain callbacks can't be serialized
- `__pregel_*` keys - Internal Pregel state
- `temporal` key in metadata - Already handled by activity options
- `timedelta` objects - Must be converted or excluded

### 8. Interrupt Handling

Graph API interrupts work via:
1. Node raises `GraphInterrupt` with value
2. Runner captures interrupt in `NodeActivityOutput.interrupt`
3. Workflow waits for signal with resume value
4. Re-execution passes resume value to the same node

Functional API interrupts work via:
1. `interrupt()` raises `GraphInterrupt`
2. Runner catches exception, extracts value
3. Calls `on_interrupt` callback if provided
4. Rebuilds config with `resume_values` in scratchpad

### 9. Store (Cross-Node Persistence)

The `ActivityLocalStore` in `_store.py` provides LangGraph's `BaseStore` interface for activities:
- Receives `StoreSnapshot` with items from workflow
- Local writes tracked in `_local_writes`
- Returns `StoreWrite` operations back to workflow
- Workflow maintains canonical store state

## File Structure

| File | Purpose |
|------|---------|
| `__init__.py` | Public API: `compile()`, `activity_options()`, `LangGraphPlugin` |
| `_plugin.py` | Unified plugin with auto-detection of graph vs entrypoint |
| `_runner.py` | Graph API runner using `AsyncPregelLoop` |
| `_functional_runner.py` | Functional API runner with `CONFIG_KEY_CALL` injection |
| `_activities.py` | Activity implementations for node execution |
| `_functional_activity.py` | Activity for @task execution |
| `_models.py` | Dataclasses for activity I/O serialization |
| `_graph_registry.py` | Registry for Graph API graphs |
| `_functional_registry.py` | Registry for Functional API entrypoints |
| `_store.py` | ActivityLocalStore implementation |
| `_exceptions.py` | Error types and non-retryable error classification |
| `_constants.py` | Shared constants |

## Common Pitfalls

### 1. step_timeout is Non-Deterministic
LangGraph's `step_timeout` uses `time.monotonic()` which varies across replays. The runner explicitly rejects graphs with `step_timeout` set.

### 2. Tasks Must Be Module-Level
For Functional API, `@task` functions must be importable:
- NOT in `__main__`
- NOT closures or lambdas
- NOT defined inside other functions

The `identifier()` function returns `None` for non-importable functions.

### 3. Parallel Branches Must Stay Parallel
When modifying `_execute_loop_tasks`, always use `asyncio.gather()` for parallel execution. Sequential execution breaks BSP semantics.

### 4. Don't Cache Loop State
`AsyncPregelLoop` instances are NOT thread-safe and should be created fresh for each invocation. Only the compiled `Pregel` graph is cacheable.

### 5. Message Type Reconstruction
When adding new channels that carry LangChain messages, ensure `ChannelWrite.create()` is used to preserve type information.

## Testing Strategy

- **Unit tests** (`test_*.py`): Test individual components with mocks
- **E2E tests** (`test_e2e_*.py`): Full workflow execution with real Temporal server
- **Samples tests** (in samples-python repo): Real-world usage patterns

Run all LangGraph tests:
```bash
uv run pytest tests/contrib/langgraph/ -v
```

## Version Compatibility

- **Python**: 3.10+
- **LangGraph**: Tests against latest, uses some deprecated APIs marked for removal in v2.0
- **Temporal SDK**: Uses `workflow.unsafe.imports_passed_through()` for sandbox compatibility
