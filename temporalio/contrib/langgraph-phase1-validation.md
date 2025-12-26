# **LangGraph Temporal Integration - Phase 1: Validation & Prototypes**

**Version:** 1.0
**Date:** 2025-01-24
**Status:** Planning
**Parent Document:** [v2 Proposal](./langgraph-plugin-proposal-v2.md)

---

## **Overview**

Phase 1 validates all technical assumptions from the proposal through throwaway prototypes and unit tests. No production code is written until all assumptions are verified.

**Principle:** Fail fast. If any core assumption is invalid, we discover it before investing in implementation.

---

## **Technical Concerns**

| # | Concern | Risk | Validation Approach |
|---|---------|------|---------------------|
| 1 | AsyncPregelLoop API | High | Prototype submit function injection |
| 2 | Write Capture | High | Prototype CONFIG_KEY_SEND callback |
| 3 | Task Interface | Medium | Inspect PregelExecutableTask structure |
| 4 | Serialization | Medium | Test state/message serialization |
| 5 | Graph Builder | Low | Test dynamic import mechanism |

---

## **Directory Structure**

```
temporalio/contrib/langgraph/
├── __init__.py                    # Empty initially
└── _prototypes/                   # THROWAWAY - deleted after Phase 1
    ├── __init__.py
    ├── pregel_loop_proto.py       # Prototype 1
    ├── write_capture_proto.py     # Prototype 2
    ├── task_inspection_proto.py   # Prototype 3
    ├── serialization_proto.py     # Prototype 4
    └── graph_builder_proto.py     # Prototype 5

tests/contrib/langgraph/
├── __init__.py
└── prototypes/                    # THROWAWAY - deleted after Phase 1
    ├── __init__.py
    ├── test_pregel_loop.py
    ├── test_write_capture.py
    ├── test_task_interface.py
    ├── test_serialization.py
    └── test_graph_builder.py
```

---

## **Prototype 1: Pregel Loop & Submit Function**

### **Concern**
The proposal assumes we can inject a custom submit function into `AsyncPregelLoop` to intercept node execution. This is the core integration point.

### **Questions to Answer**
1. What are the required constructor parameters for `AsyncPregelLoop`?
2. Can we replace/override the `submit` attribute after construction?
3. What is the exact signature of the submit function?
4. When is submit called? What arguments does it receive?
5. How do we iterate the loop and get results?

### **Prototype Code**

```python
# temporalio/contrib/langgraph/_prototypes/pregel_loop_proto.py
"""
Prototype: Validate AsyncPregelLoop submit function injection.

Questions:
1. Can we create AsyncPregelLoop with minimal parameters?
2. Can we replace the submit function?
3. What does submit receive when called?
"""

import asyncio
from typing import Any, Callable, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.pregel import Pregel


def create_simple_graph() -> Pregel:
    """Create minimal graph for testing."""

    def node_a(state: dict) -> dict:
        return {"value": state.get("value", 0) + 1}

    def node_b(state: dict) -> dict:
        return {"value": state["value"] * 2}

    graph = StateGraph(dict)
    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)

    return graph.compile()


async def test_submit_injection():
    """
    Test whether we can inject a custom submit function.

    This prototype will:
    1. Create a simple graph
    2. Try to access/replace the submit mechanism
    3. Log what submit receives
    """
    pregel = create_simple_graph()

    # Capture submit calls
    submit_calls = []

    async def custom_submit(
        fn: Callable,
        *args,
        __name__: Optional[str] = None,
        **kwargs
    ):
        """Custom submit that logs and delegates."""
        submit_calls.append({
            "fn": fn.__name__ if hasattr(fn, '__name__') else str(fn),
            "args_count": len(args),
            "args_types": [type(a).__name__ for a in args],
            "kwargs": list(kwargs.keys()),
            "__name__": __name__,
        })

        # Execute original
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)

    # TODO: Figure out how to inject custom_submit into pregel execution
    # Options to explore:
    # 1. AsyncPregelLoop constructor parameter
    # 2. Replacing loop.submit after construction
    # 3. Subclassing AsyncPregelLoop
    # 4. Using pregel.stream() with custom executor

    # For now, just run and observe
    result = await pregel.ainvoke({"value": 1})

    return {
        "result": result,
        "submit_calls": submit_calls,
    }


if __name__ == "__main__":
    output = asyncio.run(test_submit_injection())
    print("Result:", output["result"])
    print("Submit calls:", output["submit_calls"])
```

### **Test Cases**

```python
# tests/contrib/langgraph/prototypes/test_pregel_loop.py
"""
Tests for Pregel loop submit function injection.

These tests validate our assumptions about AsyncPregelLoop.
"""

import pytest
from langgraph.graph import StateGraph, START, END


class TestPregelLoopAPI:
    """Discover and validate AsyncPregelLoop API."""

    @pytest.fixture
    def simple_graph(self):
        """Create a simple 2-node graph."""
        def node_a(state: dict) -> dict:
            return {"values": state.get("values", []) + ["a"]}

        def node_b(state: dict) -> dict:
            return {"values": state["values"] + ["b"]}

        graph = StateGraph(dict)
        graph.add_node("a", node_a)
        graph.add_node("b", node_b)
        graph.add_edge(START, "a")
        graph.add_edge("a", "b")
        graph.add_edge("b", END)

        return graph.compile()

    @pytest.mark.asyncio
    async def test_basic_execution(self, simple_graph):
        """Verify basic graph execution works."""
        result = await simple_graph.ainvoke({"values": []})
        assert result["values"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_discover_loop_class(self, simple_graph):
        """Discover what loop class is used internally."""
        # Import and inspect
        from langgraph.pregel._loop import AsyncPregelLoop

        # Document constructor signature
        import inspect
        sig = inspect.signature(AsyncPregelLoop.__init__)
        params = list(sig.parameters.keys())

        print(f"AsyncPregelLoop.__init__ parameters: {params}")

        # This test documents findings, doesn't assert
        assert AsyncPregelLoop is not None

    @pytest.mark.asyncio
    async def test_submit_function_signature(self, simple_graph):
        """
        Discover submit function signature by inspecting source.

        Expected from proposal:
        async def submit(
            fn: Callable,
            *args,
            __name__: Optional[str] = None,
            __cancel_on_exit__: bool = False,
            __reraise_on_exit__: bool = True,
            __next_tick__: bool = False,
            **kwargs
        )
        """
        from langgraph.pregel._loop import AsyncPregelLoop
        import inspect

        # Check if submit is an attribute or method
        if hasattr(AsyncPregelLoop, 'submit'):
            submit_attr = getattr(AsyncPregelLoop, 'submit')
            print(f"submit type: {type(submit_attr)}")

            if callable(submit_attr):
                sig = inspect.signature(submit_attr)
                print(f"submit signature: {sig}")

        # Document findings
        assert True

    @pytest.mark.asyncio
    async def test_submit_injection_feasibility(self, simple_graph):
        """
        Test if we can inject a custom submit function.

        This is the KEY validation - if this fails, we need alternative approach.
        """
        calls_captured = []

        # Strategy 1: Try to intercept via stream with custom executor
        # Strategy 2: Subclass and override
        # Strategy 3: Monkey-patch instance

        # TODO: Implement based on discovered API

        # For now, mark as needs investigation
        pytest.skip("Requires API investigation - see prototype code")

    @pytest.mark.asyncio
    async def test_what_submit_receives(self, simple_graph):
        """
        If submit injection works, document what it receives.

        Expected from proposal:
        - fn: 'arun_with_retry' or 'run_with_retry' for node execution
        - args[0]: PregelExecutableTask
        """
        # TODO: Implement after submit injection is validated
        pytest.skip("Depends on test_submit_injection_feasibility")
```

### **Success Criteria**
- [ ] Documented AsyncPregelLoop constructor parameters
- [ ] Confirmed submit function can be replaced/injected
- [ ] Documented exact submit function signature
- [ ] Documented what fn and args contain for node execution
- [ ] Working prototype that intercepts node execution

### **Fallback Plan**
If submit injection doesn't work:
1. Explore subclassing AsyncPregelLoop
2. Explore using pregel hooks/callbacks if available
3. Explore wrapping at a higher level (node functions themselves)

---

## **Prototype 2: Write Capture Mechanism**

### **Concern**
The proposal assumes nodes write state via `CONFIG_KEY_SEND` callback, and we can capture writes by injecting our own callback.

### **Questions to Answer**
1. Does `CONFIG_KEY_SEND` exist in the config?
2. What is the callback signature?
3. What format are writes in? `[(channel, value), ...]`?
4. Do all node types (regular, ToolNode) use this mechanism?
5. Can we inject our callback and capture all writes?

### **Prototype Code**

```python
# temporalio/contrib/langgraph/_prototypes/write_capture_proto.py
"""
Prototype: Validate write capture via CONFIG_KEY_SEND.

The proposal claims:
1. Writers call config[CONF][CONFIG_KEY_SEND] callback
2. Callback receives list of (channel, value) tuples
3. We can inject our own callback to capture writes
"""

import asyncio
from collections import deque
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.pregel import Pregel

# Import the constants - verify they exist
try:
    from langgraph.constants import CONFIG_KEY_SEND, CONF
    CONSTANTS_FOUND = True
except ImportError:
    try:
        from langgraph._internal._constants import CONFIG_KEY_SEND, CONF
        CONSTANTS_FOUND = True
    except ImportError:
        CONSTANTS_FOUND = False
        CONFIG_KEY_SEND = None
        CONF = None


def test_constants_exist():
    """Verify the constants exist."""
    print(f"CONSTANTS_FOUND: {CONSTANTS_FOUND}")
    print(f"CONFIG_KEY_SEND: {CONFIG_KEY_SEND}")
    print(f"CONF: {CONF}")
    return CONSTANTS_FOUND


async def test_write_capture():
    """
    Test capturing writes via CONFIG_KEY_SEND.
    """
    if not CONSTANTS_FOUND:
        print("ERROR: Constants not found, cannot test write capture")
        return None

    # Create graph
    def add_message(state: dict) -> dict:
        return {"messages": state.get("messages", []) + ["new message"]}

    graph = StateGraph(dict)
    graph.add_node("add", add_message)
    graph.add_edge(START, "add")
    graph.add_edge("add", END)
    pregel = graph.compile()

    # Capture writes
    captured_writes: deque = deque()

    def capture_callback(writes):
        """Capture writes instead of sending to channels."""
        print(f"Captured writes: {writes}")
        captured_writes.extend(writes)

    # Try to inject callback via config
    config = {
        "configurable": {
            CONFIG_KEY_SEND: capture_callback,
        }
    }

    # Execute with custom config
    try:
        result = await pregel.ainvoke({"messages": []}, config=config)
        return {
            "result": result,
            "captured_writes": list(captured_writes),
            "success": True,
        }
    except Exception as e:
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "success": False,
        }


if __name__ == "__main__":
    print("Testing constants...")
    test_constants_exist()

    print("\nTesting write capture...")
    output = asyncio.run(test_write_capture())
    print(f"Output: {output}")
```

### **Test Cases**

```python
# tests/contrib/langgraph/prototypes/test_write_capture.py
"""
Tests for write capture mechanism.
"""

import pytest
from collections import deque


class TestWriteCapture:
    """Validate write capture via CONFIG_KEY_SEND."""

    def test_constants_importable(self):
        """Verify CONFIG_KEY_SEND and CONF can be imported."""
        try:
            from langgraph.constants import CONFIG_KEY_SEND, CONF
            found_location = "langgraph.constants"
        except ImportError:
            try:
                from langgraph._internal._constants import CONFIG_KEY_SEND, CONF
                found_location = "langgraph._internal._constants"
            except ImportError:
                pytest.fail("Could not import CONFIG_KEY_SEND and CONF")

        assert CONFIG_KEY_SEND is not None
        assert CONF is not None
        print(f"Found at: {found_location}")
        print(f"CONFIG_KEY_SEND = {CONFIG_KEY_SEND!r}")
        print(f"CONF = {CONF!r}")

    @pytest.mark.asyncio
    async def test_write_callback_injection(self):
        """Test if we can inject our own write callback."""
        from langgraph.graph import StateGraph, START, END

        # Try to import constants
        try:
            from langgraph.constants import CONFIG_KEY_SEND, CONF
        except ImportError:
            from langgraph._internal._constants import CONFIG_KEY_SEND, CONF

        captured = deque()

        def node_fn(state: dict) -> dict:
            return {"count": state.get("count", 0) + 1}

        graph = StateGraph(dict)
        graph.add_node("increment", node_fn)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)
        pregel = graph.compile()

        # Inject capture callback
        config = {
            "configurable": {
                CONFIG_KEY_SEND: captured.extend,
            }
        }

        result = await pregel.ainvoke({"count": 0}, config=config)

        print(f"Result: {result}")
        print(f"Captured: {list(captured)}")

        # Document what we captured
        # Expected: [("count", 1)] or similar

    @pytest.mark.asyncio
    async def test_write_format(self):
        """Document the exact format of captured writes."""
        # TODO: Based on test_write_callback_injection results
        pytest.skip("Depends on callback injection validation")

    @pytest.mark.asyncio
    async def test_toolnode_writes(self):
        """Test write capture with ToolNode."""
        # TODO: Test with prebuilt ToolNode
        pytest.skip("Requires ToolNode setup")
```

### **Success Criteria**
- [ ] CONFIG_KEY_SEND constant located and importable
- [ ] Callback injection via config works
- [ ] Write format documented: `[(channel, value), ...]`
- [ ] Works with regular nodes
- [ ] Works with ToolNode (if different mechanism)

---

## **Prototype 3: Task Interface Inspection**

### **Concern**
The proposal assumes specific structure of `PregelExecutableTask` including `task.proc`, `task.writes`, `task.input`, `task.config`, `task.name`.

### **Questions to Answer**
1. What attributes does PregelExecutableTask have?
2. Is `task.proc.ainvoke()` the correct invocation method?
3. Is `task.writes` a deque we can extend?
4. What does `task.input` contain?
5. What is in `task.config`?

### **Prototype Code**

```python
# temporalio/contrib/langgraph/_prototypes/task_inspection_proto.py
"""
Prototype: Inspect PregelExecutableTask structure.

We need to know the exact interface to interact with tasks
when we intercept them in the submit function.
"""

from langgraph.types import PregelExecutableTask
import inspect


def inspect_task_class():
    """Inspect PregelExecutableTask class definition."""
    print("=== PregelExecutableTask Inspection ===\n")

    # Get class attributes
    print("Class attributes:")
    for name, value in inspect.getmembers(PregelExecutableTask):
        if not name.startswith('_'):
            print(f"  {name}: {type(value).__name__}")

    # Check if it's a NamedTuple or dataclass
    print(f"\nBase classes: {PregelExecutableTask.__bases__}")

    # Get annotations
    if hasattr(PregelExecutableTask, '__annotations__'):
        print(f"\nAnnotations:")
        for name, type_hint in PregelExecutableTask.__annotations__.items():
            print(f"  {name}: {type_hint}")

    # Get fields if NamedTuple
    if hasattr(PregelExecutableTask, '_fields'):
        print(f"\nNamedTuple fields: {PregelExecutableTask._fields}")

    return PregelExecutableTask


if __name__ == "__main__":
    inspect_task_class()
```

### **Test Cases**

```python
# tests/contrib/langgraph/prototypes/test_task_interface.py
"""
Tests to document PregelExecutableTask interface.
"""

import pytest


class TestTaskInterface:
    """Document PregelExecutableTask structure."""

    def test_task_importable(self):
        """Verify PregelExecutableTask can be imported."""
        from langgraph.types import PregelExecutableTask
        assert PregelExecutableTask is not None

    def test_task_attributes(self):
        """Document task attributes."""
        from langgraph.types import PregelExecutableTask
        import inspect

        # Get source if available
        try:
            source = inspect.getsource(PregelExecutableTask)
            print("Source:")
            print(source[:500])  # First 500 chars
        except:
            print("Source not available")

        # Document structure
        if hasattr(PregelExecutableTask, '__annotations__'):
            print("\nAnnotations:")
            for k, v in PregelExecutableTask.__annotations__.items():
                print(f"  {k}: {v}")

    def test_task_proc_interface(self):
        """
        Document task.proc interface.

        Expected: task.proc should have .ainvoke() or .invoke() method
        """
        # TODO: Create actual task and inspect proc
        pytest.skip("Requires task creation via pregel execution")

    def test_task_writes_interface(self):
        """
        Document task.writes interface.

        Expected: deque that we can .extend() with (channel, value) tuples
        """
        pytest.skip("Requires task creation via pregel execution")
```

### **Success Criteria**
- [ ] Documented all PregelExecutableTask attributes
- [ ] Confirmed task.proc interface (ainvoke/invoke)
- [ ] Confirmed task.writes is extensible deque
- [ ] Documented task.input format
- [ ] Documented task.config contents

---

## **Prototype 4: State Serialization**

### **Concern**
Activity inputs/outputs must be JSON-serializable. LangGraph state may contain complex objects like LangChain messages.

### **Questions to Answer**
1. Can basic dict state be serialized?
2. Can LangChain messages (AIMessage, HumanMessage, etc.) be serialized?
3. Do we need custom Temporal payload converters?
4. What about Pydantic state models?

### **Prototype Code**

```python
# temporalio/contrib/langgraph/_prototypes/serialization_proto.py
"""
Prototype: Test serialization of LangGraph state types.
"""

import json
from typing import Any


def test_basic_dict():
    """Test basic dict serialization."""
    state = {
        "messages": ["hello", "world"],
        "count": 42,
        "nested": {"a": 1, "b": [1, 2, 3]},
    }

    serialized = json.dumps(state)
    deserialized = json.loads(serialized)

    assert state == deserialized
    print("Basic dict: OK")
    return True


def test_langchain_messages():
    """Test LangChain message serialization."""
    try:
        from langchain_core.messages import (
            HumanMessage,
            AIMessage,
            ToolMessage,
            SystemMessage,
        )
    except ImportError:
        print("langchain_core not installed")
        return None

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="Hi there!", tool_calls=[]),
        SystemMessage(content="You are helpful"),
    ]

    # Try direct JSON serialization
    try:
        serialized = json.dumps(messages)
        print("Direct JSON: OK")
    except TypeError as e:
        print(f"Direct JSON failed: {e}")

        # Try with default handler
        def message_serializer(obj):
            if hasattr(obj, 'dict'):
                return obj.dict()
            elif hasattr(obj, 'model_dump'):
                return obj.model_dump()
            raise TypeError(f"Cannot serialize {type(obj)}")

        try:
            serialized = json.dumps(messages, default=message_serializer)
            print(f"With custom serializer: OK")
            print(f"Serialized: {serialized[:200]}...")
        except Exception as e2:
            print(f"Custom serializer also failed: {e2}")
            return False

    return True


def test_temporal_serialization():
    """Test with Temporal's default converter."""
    try:
        from temporalio.converter import default

        # Test with messages
        from langchain_core.messages import HumanMessage

        msg = HumanMessage(content="test")

        # Temporal uses PayloadConverter
        payload = default().payload_converter.to_payloads([msg])
        print(f"Temporal payload created: {payload is not None}")

        # Deserialize
        result = default().payload_converter.from_payloads(payload, [HumanMessage])
        print(f"Deserialized: {result}")

    except Exception as e:
        print(f"Temporal serialization error: {e}")
        return False

    return True


if __name__ == "__main__":
    test_basic_dict()
    test_langchain_messages()
    test_temporal_serialization()
```

### **Test Cases**

```python
# tests/contrib/langgraph/prototypes/test_serialization.py
"""
Tests for state serialization.
"""

import pytest
import json


class TestSerialization:
    """Test LangGraph state serialization for Temporal."""

    def test_basic_state(self):
        """Basic dict state should serialize."""
        state = {"messages": [], "count": 0}
        assert json.loads(json.dumps(state)) == state

    @pytest.mark.skipif(
        not pytest.importorskip("langchain_core", reason="langchain_core required"),
        reason="langchain_core not installed"
    )
    def test_langchain_messages(self):
        """Test LangChain message serialization."""
        from langchain_core.messages import HumanMessage, AIMessage

        # Messages should have serialization methods
        msg = HumanMessage(content="test")

        # Check available serialization
        if hasattr(msg, 'model_dump'):
            data = msg.model_dump()
            print(f"model_dump: {data}")
        elif hasattr(msg, 'dict'):
            data = msg.dict()
            print(f"dict: {data}")

        # Verify JSON serializable
        json_str = json.dumps(data)
        assert json.loads(json_str) == data

    def test_temporal_default_converter(self):
        """Test Temporal's default payload converter."""
        from temporalio.converter import default

        # Simple data
        data = {"key": "value", "list": [1, 2, 3]}

        payloads = default().payload_converter.to_payloads([data])
        result = default().payload_converter.from_payloads(payloads, [dict])

        assert result == [data]

    def test_writes_format(self):
        """Test that writes format is serializable."""
        # Writes are [(channel, value), ...]
        writes = [
            ("messages", [{"role": "user", "content": "hi"}]),
            ("count", 5),
        ]

        # Should be JSON serializable
        json_str = json.dumps(writes)
        restored = json.loads(json_str)
        assert restored == writes
```

### **Success Criteria**
- [ ] Basic dict state serializable
- [ ] LangChain messages serializable (with or without custom converter)
- [ ] Writes format `[(channel, value)]` serializable
- [ ] Identified if custom PayloadConverter needed
- [ ] Documented serialization approach

---

## **Prototype 5: Graph Builder Import**

### **Concern**
Activities need to reconstruct the graph. The proposal suggests importing a graph builder function by module path.

### **Questions to Answer**
1. Can we reliably import a function by module path?
2. Does the reconstructed graph have equivalent nodes?
3. Should we pass builder path as activity argument or use registry?
4. How to handle graphs defined in `__main__`?

### **Prototype Code**

```python
# temporalio/contrib/langgraph/_prototypes/graph_builder_proto.py
"""
Prototype: Test graph reconstruction in activities.
"""

import importlib
from typing import Callable


def import_function(module_path: str) -> Callable:
    """
    Import a function by its full module path.

    Args:
        module_path: e.g., "my_module.build_graph"

    Returns:
        The imported function
    """
    module_name, func_name = module_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


# Registry alternative
GRAPH_REGISTRY: dict[str, Callable] = {}


def register_graph(name: str):
    """Decorator to register graph builder."""
    def decorator(fn: Callable) -> Callable:
        GRAPH_REGISTRY[name] = fn
        return fn
    return decorator


def get_graph_builder(name: str) -> Callable:
    """Get builder from registry."""
    if name not in GRAPH_REGISTRY:
        raise KeyError(f"Graph '{name}' not registered")
    return GRAPH_REGISTRY[name]


# Test functions
@register_graph("test_graph")
def build_test_graph():
    """Example graph builder."""
    from langgraph.graph import StateGraph, START, END

    graph = StateGraph(dict)
    graph.add_node("a", lambda s: {"x": 1})
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    return graph.compile()


if __name__ == "__main__":
    # Test registry approach
    builder = get_graph_builder("test_graph")
    graph = builder()
    print(f"Registry approach: {graph}")

    # Test import approach (would need actual module path)
    # builder = import_function("my_package.my_module.build_graph")
```

### **Test Cases**

```python
# tests/contrib/langgraph/prototypes/test_graph_builder.py
"""
Tests for graph reconstruction mechanisms.
"""

import pytest


class TestGraphBuilder:
    """Test graph builder import/registry mechanisms."""

    def test_registry_approach(self):
        """Test registry-based graph builder lookup."""
        from temporalio.contrib.langgraph._prototypes.graph_builder_proto import (
            register_graph,
            get_graph_builder,
            GRAPH_REGISTRY,
        )
        from langgraph.graph import StateGraph, START, END

        @register_graph("my_test_graph")
        def build():
            graph = StateGraph(dict)
            graph.add_node("n", lambda s: s)
            graph.add_edge(START, "n")
            graph.add_edge("n", END)
            return graph.compile()

        # Retrieve and build
        builder = get_graph_builder("my_test_graph")
        graph = builder()

        assert graph is not None
        assert "n" in graph.nodes

    def test_import_approach(self):
        """Test import-based graph builder lookup."""
        import importlib

        # This would work for module-level functions
        # e.g., "myapp.graphs.build_agent_graph"

        # For testing, we use a known module
        module = importlib.import_module("langgraph.graph")
        StateGraph = getattr(module, "StateGraph")

        assert StateGraph is not None

    def test_graph_equivalence(self):
        """Test that rebuilt graph has same structure."""
        from langgraph.graph import StateGraph, START, END

        def build():
            graph = StateGraph(dict)
            graph.add_node("a", lambda s: {"v": 1})
            graph.add_node("b", lambda s: {"v": 2})
            graph.add_edge(START, "a")
            graph.add_edge("a", "b")
            graph.add_edge("b", END)
            return graph.compile()

        g1 = build()
        g2 = build()

        # Same nodes
        assert set(g1.nodes.keys()) == set(g2.nodes.keys())

        # Same structure
        assert g1.input_channels == g2.input_channels
        assert g1.output_channels == g2.output_channels

    def test_recommendation(self):
        """Document recommended approach."""
        # Registry pros:
        # - Works with lambdas
        # - No module path management
        # - Clear registration point

        # Import pros:
        # - No global state
        # - Works across processes automatically
        # - Standard Python pattern

        # Recommendation: Support both, prefer import for production
        print("Recommendation: Import approach with registry fallback")
```

### **Success Criteria**
- [ ] Import approach works for module-level functions
- [ ] Registry approach works for all function types
- [ ] Reconstructed graph has equivalent nodes
- [ ] Chosen recommended approach
- [ ] Documented limitations (e.g., `__main__` graphs)

---

## **Commit Plan**

| # | Commit | Description | Validates |
|---|--------|-------------|-----------|
| 1 | Setup prototype structure | Create directories and empty files | - |
| 2 | Pregel loop prototype | Implement and test submit injection | Concern #1 |
| 3 | Write capture prototype | Implement and test CONFIG_KEY_SEND | Concern #2 |
| 4 | Task interface prototype | Inspect and document task structure | Concern #3 |
| 5 | Serialization prototype | Test state/message serialization | Concern #4 |
| 6 | Graph builder prototype | Test import/registry approaches | Concern #5 |
| 7 | Validation summary | Document findings, update proposal | All |

---

## **Exit Criteria**

Phase 1 is complete when:

- [ ] All 5 prototypes implemented
- [ ] All test cases pass or have documented workarounds
- [ ] Validation summary document created
- [ ] v2 proposal updated with any corrections
- [ ] Decision made on any alternative approaches needed
- [ ] Green light to proceed to Phase 2

---

## **Risk Mitigation**

| Risk | Mitigation |
|------|------------|
| Submit injection doesn't work | Explore subclassing, hooks, or node wrapping |
| Write capture mechanism different | Inspect actual Pregel source, adapt approach |
| Serialization complex | Design custom PayloadConverter |
| Graph reconstruction unreliable | Use registry with explicit registration |

---

**End of Document**
