# LangGraph Interrupt API Design

**Date:** 2025-01-25
**Status:** Implemented
**Scope:** Runner API for human-in-the-loop workflows

---

## Overview

This document describes the interrupt API for the LangGraph-Temporal integration, enabling human-in-the-loop workflows. The API matches LangGraph's native behavior exactly - interrupts are returned as `__interrupt__` in the result dict, not raised as exceptions.

---

## API

### Interrupt Return Value

When a LangGraph node calls `interrupt()`, `ainvoke()` returns a dict containing `__interrupt__`:

```python
result = await app.ainvoke(input_state)

if '__interrupt__' in result:
    # Interrupt occurred - result['__interrupt__'] is a list of Interrupt objects
    interrupt_info = result['__interrupt__'][0]
    interrupt_value = interrupt_info.value  # Value passed to interrupt()
```

This matches LangGraph's native API exactly.

### Resuming with Command

To resume after an interrupt, use LangGraph's `Command` class:

```python
from langgraph.types import Command

# Resume with a value
result = await app.ainvoke(Command(resume=human_input))
```

---

## Usage Examples

### Example 1: Simple Approval with Signal

```python
from temporalio import workflow
from temporalio.contrib.langgraph import compile
from langgraph.types import Command


@workflow.defn
class ApprovalWorkflow:
    def __init__(self):
        self._approved: bool | None = None

    @workflow.signal
    def approve(self, approved: bool):
        self._approved = approved

    @workflow.run
    async def run(self, request: dict) -> dict:
        app = compile("approval_graph")

        result = await app.ainvoke(request)

        # Check for interrupt (matches LangGraph native API)
        if '__interrupt__' in result:
            interrupt_info = result['__interrupt__'][0]
            workflow.logger.info(f"Waiting for approval: {interrupt_info.value}")

            # Wait for signal
            await workflow.wait_condition(lambda: self._approved is not None)

            # Resume with the approval decision
            result = await app.ainvoke(Command(resume=self._approved))

        return result
```

### Example 2: Tool Approval with Update

```python
from temporalio import workflow
from temporalio.contrib.langgraph import compile
from langgraph.types import Command


@workflow.defn
class AgentWorkflow:
    def __init__(self):
        self._tool_response: dict | None = None
        self._pending_tool: dict | None = None

    @workflow.update
    async def review_tool_call(self, decision: dict) -> str:
        self._tool_response = decision
        return "received"

    @workflow.query
    def get_pending_tool(self) -> dict | None:
        return self._pending_tool

    @workflow.run
    async def run(self, query: str) -> dict:
        app = compile("agent_graph")
        state = {"messages": [{"role": "user", "content": query}]}

        result = await app.ainvoke(state)

        if '__interrupt__' in result:
            # Store interrupt info for query
            self._pending_tool = result['__interrupt__'][0].value

            # Wait for update
            await workflow.wait_condition(lambda: self._tool_response is not None)
            response = self._tool_response
            self._tool_response = None
            self._pending_tool = None

            # Resume with the tool decision
            result = await app.ainvoke(Command(resume=response))

        return result
```

### Example 3: Multiple Interrupts

```python
from temporalio import workflow
from temporalio.contrib.langgraph import compile
from langgraph.types import Command


@workflow.defn
class MultiStepWorkflow:
    def __init__(self):
        self._response: Any = None

    @workflow.signal
    def provide_input(self, value: Any):
        self._response = value

    @workflow.run
    async def run(self, input_state: dict) -> dict:
        app = compile("multi_step_graph")

        # Handle multiple potential interrupts
        current_input: dict | Command = input_state

        while True:
            result = await app.ainvoke(current_input)

            if '__interrupt__' not in result:
                return result

            interrupt_info = result['__interrupt__'][0]
            workflow.logger.info(f"Interrupt: {interrupt_info.value}")

            # Wait for human input
            await workflow.wait_condition(lambda: self._response is not None)

            # Resume with Command
            current_input = Command(resume=self._response)
            self._response = None
```

### Example 4: External Approval System

```python
from temporalio import workflow
from temporalio.contrib.langgraph import compile
from langgraph.types import Command


@workflow.defn
class ExternalApprovalWorkflow:
    @workflow.run
    async def run(self, input_state: dict) -> dict:
        app = compile("my_graph")

        result = await app.ainvoke(input_state)

        if '__interrupt__' in result:
            interrupt_info = result['__interrupt__'][0]

            # Call external approval system via activity
            approval = await workflow.execute_activity(
                request_external_approval,
                interrupt_info.value,
                start_to_close_timeout=timedelta(hours=24),
            )

            # Resume with approval result
            result = await app.ainvoke(Command(resume=approval))

        return result
```

---

## How It Works

### Execution Flow

```
1. Workflow calls app.ainvoke(input_state)
   │
2. Runner executes Pregel loop, calling activities for each node
   │
3. Activity executes node, which calls interrupt(value)
   │
4. Activity catches LangGraph's GraphInterrupt, returns InterruptValue
   │
5. Runner detects interrupt, saves state, returns result with __interrupt__
   │
6. Workflow checks for __interrupt__ in result
   │
7. Workflow handles human input (signals/updates/etc)
   │
8. Workflow calls app.ainvoke(Command(resume=value))
   │
9. Runner extracts resume value, uses saved state, re-executes
   │
10. Activity executes node again with resume value in config
    │
11. Node's interrupt() returns resume value instead of raising
    │
12. Node completes, writes are captured, execution continues
    │
13. Final result returned to workflow (without __interrupt__)
```

### State Management

When an interrupt occurs:
1. The interrupted node's input state is saved in `_interrupted_state`
2. The result is returned with `__interrupt__` key containing LangGraph `Interrupt` objects
3. When `Command(resume=value)` is passed, the saved state is used
4. The graph re-executes from this state with the resume value

---

## Implementation Details

### Models (`_models.py`)

```python
class InterruptValue(BaseModel):
    """Data about an interrupt raised by a node."""
    value: Any
    node_name: str
    task_id: str


class NodeActivityOutput(BaseModel):
    writes: list[ChannelWrite]
    interrupt: Optional[InterruptValue] = None  # Set if node interrupted
```

### Activity (`_activities.py`)

The activity catches LangGraph's internal `GraphInterrupt` and returns it as `InterruptValue`:

```python
try:
    # Execute node
    await node_runnable.ainvoke(input_state, config)
except LangGraphInterrupt as e:
    # Extract value from Interrupt object
    interrupt_value = e.args[0][0].value if e.args else None
    return NodeActivityOutput(
        writes=[],
        interrupt=InterruptValue(
            value=interrupt_value,
            node_name=input_data.node_name,
            task_id=input_data.task_id,
        ),
    )
```

### Runner (`_runner.py`)

The runner detects interrupts and returns them in the result (matching native LangGraph API):

```python
async def ainvoke(self, input_state, config=None):
    # Check if input is a Command with resume value
    if isinstance(input_state, Command):
        if hasattr(input_state, "resume") and input_state.resume is not None:
            resume_value = input_state.resume
        input_state = self._interrupted_state  # Use saved state

    # ... execute graph ...

    # Get output from loop
    output = loop.output or {}

    # If there's a pending interrupt, add it to the result (LangGraph native API)
    if self._pending_interrupt is not None:
        interrupt_obj = Interrupt.from_ns(
            value=self._pending_interrupt.value,
            ns="",
        )
        output = {**output, "__interrupt__": [interrupt_obj]}

    return output
```

---

## Comparison with Native LangGraph API

This implementation matches LangGraph's native behavior exactly:

| Feature | Native LangGraph | Temporal Integration |
|---------|------------------|----------------------|
| Interrupt detection | Check `'__interrupt__' in result` | Same |
| Interrupt value | `result['__interrupt__'][0].value` | Same |
| Resume | `app.invoke(Command(resume=value))` | Same |
| Return type | Dict with state + optional `__interrupt__` | Same |

---

## Limitations

1. **Single interrupt at a time**: If multiple nodes interrupt in parallel, only one is surfaced. This matches LangGraph's behavior.

2. **State at interrupt point**: The saved state is the input to the interrupted node, not the full graph state. For complex graphs, consider using LangGraph checkpointing (future feature).

3. **No checkpointing**: This implementation doesn't use LangGraph's checkpointer. The state is stored in the runner instance within the workflow.

---

## Future Enhancements

1. **Full checkpointing support**: Integrate with LangGraph's checkpointer for cross-workflow state persistence
2. **Multiple interrupt handling**: Queue multiple interrupts if parallel nodes interrupt
3. **Interrupt timeout**: Optional timeout for waiting on interrupts

---

## References

- [LangGraph Interrupts Documentation](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/wait-user-input/)
- [LangGraph Command API](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)

---

**End of Document**
