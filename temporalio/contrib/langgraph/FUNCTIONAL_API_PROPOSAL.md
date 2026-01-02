# LangGraph Functional API + Temporal Integration Proposal

This document proposes integration between LangGraph's Functional API and Temporal using `LangGraphFunctionalPlugin`.

> ⚠️ **Note**: `LangGraphFunctionalPlugin` is a **proposal** and does not exist yet. This document shows the intended developer experience and implementation approach.

## Technical Analysis

### Verified LangGraph APIs

The following APIs have been verified to exist and work as expected:

| API | Location | Purpose |
|-----|----------|---------|
| `@task` decorator | `langgraph/func/__init__.py:115` | Wraps functions as tasks |
| `@entrypoint` decorator | `langgraph/func/__init__.py:228` | Returns `Pregel` object |
| `CONFIG_KEY_CALL` | `langgraph/_internal/_constants.py:34` | Key for task routing callback |
| `call()` function | `langgraph/pregel/_call.py:253` | Routes task calls via config |
| `identifier()` function | `langgraph/pregel/_call.py:79` | Returns `module.qualname` string |
| `interrupt()` function | `langgraph/types.py:401` | Human-in-the-loop interrupts |
| `var_child_runnable_config` | `langchain_core.runnables.config` | Context var for config injection |

### Key Technical Insight: CONFIG_KEY_CALL Injection

LangGraph's `@task` decorator creates a `_TaskFunction` that calls `call()` when invoked:

```python
# langgraph/pregel/_call.py:253-269
def call(func, *args, retry_policy=None, cache_policy=None, **kwargs):
    config = get_config()  # Reads from var_child_runnable_config context var
    impl = config[CONF][CONFIG_KEY_CALL]  # Gets the routing callback
    fut = impl(func, (args, kwargs), retry_policy=retry_policy, ...)
    return fut
```

**Critical**: `CONFIG_KEY_CALL` is read from a **context variable** (`var_child_runnable_config`), not set at compile time. To intercept task calls, we must:

1. Set the context var with our custom config before running the entrypoint
2. Provide a callback that routes to Temporal activities
3. Return a `SyncAsyncFuture` that resolves when the activity completes

### Constraints and Requirements

1. **Tasks must be module-level importable functions**
   - `identifier()` returns `None` for functions defined in `__main__`, closures, or lambdas
   - Dynamic activity dispatch requires `module.qualname` format

2. **Config injection requires context var manipulation**
   - Must use `var_child_runnable_config.set()` before execution
   - Must provide all required config keys (`CONFIG_KEY_CALL`, `CONFIG_KEY_SEND`, etc.)

3. **Future type must match LangGraph expectations**
   - The callback must return `SyncAsyncFuture[T]` (supports both sync and async awaiting)

---

## Design Approach

### Core Principle: Entrypoints Run in Workflow Sandbox

The `@entrypoint` function runs **directly in the Temporal workflow sandbox**, not in an activity. This works because:

1. **LangGraph modules passed through sandbox** - `langgraph`, `langchain_core`, `pydantic_core`, etc.
2. **LangGraph machinery is deterministic** - `Pregel`, `call()`, channel operations are all deterministic
3. **@task calls routed to activities** - via `CONFIG_KEY_CALL` callback injection
4. **Sandbox enforces determinism** - `time.time()`, `random()`, etc. in entrypoint code is rejected

This aligns with LangGraph's own checkpoint/replay model where:
- Task results are cached for replay
- Entrypoint control flow must be deterministic
- Non-deterministic operations belong in tasks

### Execution Model

```
┌─────────────────────────────────────────────────────────────┐
│  Temporal Workflow (sandbox)                                │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  @entrypoint function (runs in workflow sandbox)      │  │
│  │                                                       │  │
│  │  research = await research_topic(topic)               │  │
│  │              └──── CONFIG_KEY_CALL ──────────────────────► Activity
│  │                                                       │  │
│  │  intro = write_section(topic, "intro", research)      │  │
│  │  body = write_section(topic, "body", research)        │  │
│  │          └──── CONFIG_KEY_CALL ──────────────────────────► Activities
│  │                                                       │  │  (parallel)
│  │  sections = [await intro, await body]                 │  │
│  │                                                       │  │
│  │  return {"sections": sections}                        │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Why This Design?

**Option rejected: Entrypoint as activity**
- Activities can't schedule other activities
- Would lose per-task durability
- All tasks would be local function calls

**Option chosen: Entrypoint in workflow sandbox**
- Matches LangGraph's determinism expectations
- Per-task durability via activities
- Sandbox catches non-deterministic code
- Same pattern as graph API (traversal in workflow, nodes as activities)

---

## Developer Experience

### 1. Define Tasks

```python
# tasks.py
from langgraph.func import task

@task
async def research_topic(topic: str) -> dict:
    """Runs as Temporal activity. Can use time, random, I/O, etc."""
    return {"facts": [...]}

@task
async def write_section(topic: str, section: str, research: dict) -> str:
    """Non-deterministic operations belong here."""
    return f"Content about {topic}..."
```

> **Requirement**: Tasks MUST be defined at module level (not inside functions or classes) so `identifier()` can resolve them.

### 2. Define Entrypoints

```python
# entrypoint.py
from langgraph.func import entrypoint
from langgraph.types import interrupt
from .tasks import research_topic, write_section

@entrypoint()
async def document_workflow(topic: str) -> dict:
    """Runs in workflow sandbox. Must be deterministic."""
    # Task calls become activities
    research = await research_topic(topic)

    # Parallel execution
    intro = write_section(topic, "intro", research)
    body = write_section(topic, "body", research)
    sections = [await intro, await body]

    # Control flow is deterministic
    return {"sections": sections}

@entrypoint()
async def review_workflow(topic: str) -> dict:
    """Entrypoint with human-in-the-loop."""
    draft = await generate_draft(topic)

    # interrupt() handled by workflow's on_interrupt callback
    review = interrupt({"document": draft, "action": "review"})

    return {"status": review["decision"], "document": draft}
```

### 3. Define Temporal Workflows

```python
# workflow.py
from temporalio import workflow
from temporalio.contrib.langgraph import compile

@workflow.defn
class DocumentWorkflow:
    @workflow.run
    async def run(self, topic: str) -> dict:
        app = compile("document_workflow")
        # Entrypoint runs in workflow, tasks become activities
        result = await app.ainvoke(topic)
        return result

@workflow.defn
class ReviewWorkflow:
    """Full control over Temporal features."""

    def __init__(self):
        self._resume_value = None

    @workflow.signal
    async def resume(self, value: dict) -> None:
        self._resume_value = value

    @workflow.query
    def get_status(self) -> dict:
        return {"waiting": self._waiting}

    @workflow.run
    async def run(self, topic: str) -> dict:
        app = compile("review_workflow")
        result = await app.ainvoke(
            topic,
            on_interrupt=self._handle_interrupt,
        )
        return result

    async def _handle_interrupt(self, value: dict) -> dict:
        self._waiting = True
        await workflow.wait_condition(lambda: self._resume_value is not None)
        self._waiting = False
        return self._resume_value
```

### 4. Register with Plugin

```python
# run_worker.py
from temporalio.contrib.langgraph import LangGraphFunctionalPlugin

# NO explicit task registration - discovered dynamically!
plugin = LangGraphFunctionalPlugin(
    entrypoints=[document_workflow, review_workflow],
    default_task_timeout=timedelta(minutes=10),
    task_options={
        "research_topic": {
            "start_to_close_timeout": timedelta(minutes=15),
        },
    },
)

# Plugin configures sandbox passthrough automatically
client = await Client.connect("localhost:7233", plugins=[plugin])

worker = Worker(
    client,
    task_queue="langgraph-functional",
    workflows=[DocumentWorkflow, ReviewWorkflow],
)
```

---

## Implementation Details

### Plugin Responsibilities

```python
class LangGraphFunctionalPlugin(SimplePlugin):
    def __init__(self, entrypoints, ...):
        # 1. Register entrypoints by name (extracted from __name__)
        for ep in entrypoints:
            register_entrypoint(ep.__name__, ep)

        # 2. Configure sandbox passthrough
        def workflow_runner(runner):
            return runner.with_passthrough_modules(
                "pydantic_core", "langchain_core",
                "annotated_types", "langgraph"
            )

        # 3. Provide dynamic task activity
        def add_activities(activities):
            return list(activities) + [execute_langgraph_task]

        # 4. Configure data converter
        super().__init__(
            workflow_runner=workflow_runner,
            activities=add_activities,
            data_converter=pydantic_converter,
        )
```

### CONFIG_KEY_CALL Injection (Detailed)

The `compile()` function returns a `TemporalFunctionalRunner` that wraps the Pregel and injects the config:

```python
from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import (
    CONF, CONFIG_KEY_CALL, CONFIG_KEY_SEND, CONFIG_KEY_READ,
    CONFIG_KEY_SCRATCHPAD, CONFIG_KEY_RUNTIME,
)
from langgraph._internal._scratchpad import PregelScratchpad
from langgraph.pregel._call import identifier

class TemporalFunctionalRunner:
    def __init__(self, pregel: Pregel, graph_id: str, ...):
        self.pregel = pregel
        self.graph_id = graph_id
        self._task_options = task_options or {}

    async def ainvoke(self, input_state, config=None, on_interrupt=None):
        # Prepare writes capture
        writes: list[tuple[str, Any]] = []

        # Create the temporal call callback
        def temporal_call_callback(func, input, retry_policy=None,
                                   cache_policy=None, callbacks=None):
            task_id = identifier(func)
            if task_id is None:
                raise ValueError(
                    f"Cannot route task {func} to activity: not importable. "
                    "Tasks must be module-level functions."
                )

            # Get task-specific options
            task_opts = self._task_options.get(task_id.rsplit(".", 1)[-1], {})
            timeout = task_opts.get(
                "start_to_close_timeout",
                timedelta(minutes=5)
            )

            # Create a future that will resolve when activity completes
            future = TemporalActivityFuture()

            # Schedule the activity (this is workflow code)
            async def schedule():
                result = await workflow.execute_activity(
                    execute_langgraph_task,
                    args=TaskActivityInput(
                        task_id=task_id,
                        args=input[0],  # positional args
                        kwargs=input[1],  # keyword args
                    ),
                    start_to_close_timeout=timeout,
                )
                future.set_result(result)

            # Start scheduling (non-blocking)
            asyncio.create_task(schedule())
            return future

        # Build the config with our callback
        injected_config = {
            "configurable": {
                CONFIG_KEY_CALL: temporal_call_callback,
                CONFIG_KEY_SEND: writes.append,
                CONFIG_KEY_READ: lambda ch, fresh=False: {},  # Simplified
                CONFIG_KEY_SCRATCHPAD: PregelScratchpad(),
                # ... other required keys
            }
        }

        # Merge with user config
        if config:
            injected_config = {**config, **injected_config}

        # Set context var and run
        token = var_child_runnable_config.set(injected_config)
        try:
            # Run the Pregel (entrypoint)
            result = await self.pregel.ainvoke(input_state, injected_config)

            # Handle interrupts
            if "__interrupt__" in result and on_interrupt:
                resume_value = await on_interrupt(result["__interrupt__"])
                # Re-run with resume value...

            return result
        finally:
            var_child_runnable_config.reset(token)
```

### TemporalActivityFuture Implementation

The future must support both sync `.result()` and async `await`:

```python
class TemporalActivityFuture(Generic[T]):
    """Future that bridges Temporal activities with LangGraph's SyncAsyncFuture."""

    def __init__(self):
        self._result: T | None = None
        self._exception: BaseException | None = None
        self._done = False
        self._event = asyncio.Event()

    def set_result(self, result: T) -> None:
        self._result = result
        self._done = True
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._done = True
        self._event.set()

    def result(self, timeout: float | None = None) -> T:
        """Sync result access - blocks until complete."""
        if not self._done:
            # In workflow context, this needs special handling
            raise RuntimeError("Cannot block in workflow - use await instead")
        if self._exception:
            raise self._exception
        return cast(T, self._result)

    def __await__(self) -> Generator[Any, None, T]:
        """Async await support."""
        yield from self._event.wait().__await__()
        if self._exception:
            raise self._exception
        return cast(T, self._result)
```

### Dynamic Task Activity

```python
@activity.defn
async def execute_langgraph_task(input: TaskActivityInput) -> Any:
    """Execute any @task function by module path."""
    task_id = input.task_id

    # Parse module and function name
    module_name, func_name = task_id.rsplit(".", 1)

    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
    except (ImportError, AttributeError) as e:
        raise ApplicationError(
            f"Cannot import task {task_id}: {e}",
            type="TASK_NOT_FOUND",
            non_retryable=True,
        )

    # Get the underlying function from _TaskFunction wrapper
    if hasattr(func, 'func'):
        actual_func = func.func
    else:
        actual_func = func

    # Execute the task
    if asyncio.iscoroutinefunction(actual_func):
        result = await actual_func(*input.args, **input.kwargs)
    else:
        result = actual_func(*input.args, **input.kwargs)

    return result
```

---

## Determinism Rules

### ✅ Allowed in @entrypoint (runs in workflow)
- Control flow: `if`, `for`, `while`, `match`
- Task calls: `await my_task(...)`
- Parallel tasks: `asyncio.gather(*[task1(), task2()])`
- `interrupt()` for human-in-the-loop
- Pure functions, data transformations

### ❌ Not allowed in @entrypoint (sandbox rejects)
- `time.time()`, `datetime.now()`
- `random.random()`, `uuid.uuid4()`
- Network I/O, file I/O
- Non-deterministic libraries

### ✅ Allowed in @task (runs as activity)
- Everything! Tasks are activities with no sandbox restrictions
- API calls, database access, file I/O
- Time, random, UUIDs
- Any non-deterministic operation

---

## Design Decisions (Resolved)

### 1. Sync `.result()` Support - Workflow-Aware Future ✅

LangGraph's `SyncAsyncFuture` extends `concurrent.futures.Future[T]`:

```python
class SyncAsyncFuture(Generic[T], concurrent.futures.Future[T]):
    def __await__(self) -> Generator[T, None, T]:
        yield cast(T, ...)
```

**Solution**: Create a workflow-aware future that inherits from `concurrent.futures.Future` but uses Temporal's event mechanism instead of blocking:

```python
class TemporalTaskFuture(Generic[T], concurrent.futures.Future[T]):
    """Future that works in Temporal workflow context without blocking."""

    def __init__(self):
        super().__init__()
        self._temporal_handle: workflow.ActivityHandle | None = None
        self._result_ready = False
        self._result_value: T | None = None
        self._exception: BaseException | None = None

    def result(self, timeout: float | None = None) -> T:
        """Non-blocking result access using Temporal's async primitives.

        When called in a workflow, this uses workflow.wait_condition
        under the hood to avoid blocking the event loop.
        """
        if self._exception:
            raise self._exception
        if self._result_ready:
            return cast(T, self._result_value)

        # In workflow context, this gets special handling by the sandbox
        # The workflow runtime intercepts this and handles it correctly
        if self._temporal_handle:
            # The handle's result is awaitable - sandbox converts this
            return self._temporal_handle.result()

        raise RuntimeError("Future not properly initialized")

    def __await__(self) -> Generator[Any, None, T]:
        """Async await support."""
        if self._temporal_handle:
            return (yield from self._temporal_handle.__await__())
        if self._result_ready:
            return cast(T, self._result_value)
        raise RuntimeError("Future not properly initialized")
```

**User code patterns**:
```python
result = await my_task(x)       # ✅ Async pattern - always works
result = my_task(x).result()   # ⚠️ Sync pattern - see note below
```

> **Note on `.result()` in workflows**: The sync `.result()` pattern presents a challenge in Temporal workflows because `concurrent.futures.Future.result()` is inherently blocking. Options:
>
> 1. **Raise helpful error**: `.result()` in workflow context raises `RuntimeError("Use 'await' in Temporal workflows")`
> 2. **Workflow-aware future**: If the Temporal SDK's `ActivityHandle` exposes a sync-compatible interface, we could delegate to it
> 3. **Documentation**: Clearly document that `.result()` works in activities but workflows should use `await`
>
> Since LangGraph's functional API examples predominantly use `await` syntax, this limitation may be acceptable.

### 2. Interrupt Handling - Already Solved ✅

The graph-based plugin already implements this pattern (see `approval_graph_interrupt` example):

```python
@workflow.run
async def run(self, request: ApprovalRequest) -> dict[str, Any]:
    self._app = lg_compile("approval_workflow")

    # First invocation - hits interrupt
    result = await self._app.ainvoke(initial_state)

    if "__interrupt__" in result:
        # Store interrupt value for queries
        self._interrupt_value = result["__interrupt__"][0].value

        # Wait for signal with human input
        await workflow.wait_condition(
            lambda: self._approval_response is not None,
            timeout=approval_timeout,
        )

        # Resume with Command
        result = await self._app.ainvoke(Command(resume=self._approval_response))

    return result
```

The functional API uses the **same pattern** - the runner returns `__interrupt__` in results, and the workflow handles resume via `Command(resume=value)`.

### 3. Two Task Types - In-Workflow vs In-Activity ✅

Support two execution modes for tasks:

#### In-Workflow Tasks (Default)
Tasks called from `@entrypoint` run as **activities**:
```python
@entrypoint()
async def my_workflow(x: int) -> int:
    # This task call becomes a Temporal activity
    result = await compute_task(x)
    return result
```

#### In-Activity Tasks (Nested Calls)
When a task calls another task **inside an activity**, the nested task runs **inline** in the same activity:

```python
@task
async def outer_task(x: int) -> int:
    # This runs INLINE - not as a separate activity
    # (because we're already in an activity)
    return await inner_task(x + 1)

@task
async def inner_task(x: int) -> int:
    return x * 2
```

**Implementation**: The `CONFIG_KEY_CALL` callback detects execution context:
- In workflow → schedule as activity
- In activity → execute inline (or optionally send workflow update)

```python
def temporal_call_callback(func, input, ...):
    if workflow.unsafe.is_workflow_context():
        # Schedule as activity
        return schedule_activity(func, input)
    else:
        # We're in an activity - execute inline
        return execute_inline(func, input)
```

**Alternative for long-running nested tasks**: Use workflow update to request the workflow schedule another activity. This allows an activity to delegate work back to the workflow for durability.

### 4. Error Propagation - Standard Python Exceptions ✅

LangGraph uses standard Python exceptions:
- `GraphRecursionError(RecursionError)` - recursion limit exceeded
- `InvalidUpdateError(Exception)` - invalid channel updates
- `GraphInterrupt(GraphBubbleUp)` - internal interrupt signaling
- `EmptyInputError(Exception)` - empty graph input
- `TaskNotFound(Exception)` - task not found (distributed mode)

**These propagate naturally** through the Temporal activity system. When an activity raises an exception, Temporal wraps it in `ActivityError` which preserves the original exception type and message.

The plugin should:
1. Let LangGraph exceptions bubble up from activities normally
2. Unwrap `ActivityError` when needed to expose the original exception
3. Map activity failures to appropriate retry behavior based on exception type

```python
@activity.defn
async def execute_langgraph_task(input: TaskActivityInput) -> Any:
    try:
        result = await execute_task(input)
        return result
    except GraphRecursionError:
        # Non-retryable - re-raise as ApplicationError
        raise ApplicationError(
            "Graph recursion limit exceeded",
            type="GRAPH_RECURSION_ERROR",
            non_retryable=True,
        )
    except Exception as e:
        # Other errors - let Temporal handle retry
        raise
```

---

## Comparison with Graph API

| Aspect | Graph API | Functional API |
|--------|-----------|----------------|
| Definition | `StateGraph` + `add_node()` | `@task` + `@entrypoint` |
| Control flow | Graph edges | Python code |
| Execution context | Traversal in workflow, nodes as activities | Entrypoint in workflow, tasks as activities |
| In-workflow API | `compile("name")` | `compile("name")` |
| Activity discovery | From graph nodes | Dynamic via `CONFIG_KEY_CALL` |
| Registration | `graphs={name: builder}` | `entrypoints=[func, ...]` |
| Sandbox | Passthrough for langchain_core | + passthrough for langgraph |
| Task constraints | Any callable | Must be module-level importable |

---

## Sample Structure

```
functional_api_proposal/
├── tasks.py          # @task functions (→ activities, can be non-deterministic)
├── entrypoint.py     # @entrypoint functions (→ run in workflow, must be deterministic)
├── workflow.py       # User-defined Temporal workflows
├── run_worker.py     # Plugin setup
├── run_workflow.py   # Execute workflows
└── README.md
```

## Running the Sample

```bash
# 1. Start Temporal
temporal server start-dev

# 2. Start Worker
python -m langgraph_plugin.functional_api_proposal.run_worker

# 3. Run Workflows
python -m langgraph_plugin.functional_api_proposal.run_workflow document
python -m langgraph_plugin.functional_api_proposal.run_workflow review
```
