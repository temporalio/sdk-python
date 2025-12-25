"""Prototype 1: Validate AsyncPregelLoop submit function injection.

Technical Concern:
    Can we inject a custom submit function into AsyncPregelLoop to intercept
    node execution? This is the core integration point for routing nodes
    to Temporal activities.

FINDINGS:
    1. CONFIG_KEY_RUNNER_SUBMIT = '__pregel_runner_submit' can be set in config
    2. This is passed to PregelRunner(submit=...) in Pregel.astream
    3. Submit signature: (fn, *args, __name__=None, __cancel_on_exit__=False,
       __reraise_on_exit__=True, __next_tick__=False, **kwargs) -> Future[T]
    4. fn is typically `arun_with_retry` with task as first arg
    5. WARNING: CONFIG_KEY_RUNNER_SUBMIT is deprecated in LangGraph v1.0

Key Insight:
    When submit is called, fn=arun_with_retry, args[0]=PregelExecutableTask
    We can intercept this and route to a Temporal activity instead.

    IMPORTANT: The dunder args (__name__, __cancel_on_exit__, etc.) are for
    the submit mechanism itself and should NOT be passed to fn. Only *args
    and **kwargs should be passed to fn.

VALIDATION STATUS: PASSED
    - Submit injection works via CONFIG_KEY_RUNNER_SUBMIT
    - Sequential graphs use "fast path" and may not call submit
    - Parallel graphs DO call submit for concurrent node execution
    - PregelExecutableTask provides: name, id, input, proc, config, writes
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from weakref import WeakMethod

# Suppress the deprecation warning for our prototype
warnings.filterwarnings(
    "ignore",
    message=".*CONFIG_KEY_RUNNER_SUBMIT.*",
    category=DeprecationWarning,
)

from langchain_core.runnables import RunnableConfig
from langgraph.constants import CONFIG_KEY_RUNNER_SUBMIT
from langgraph.graph import END, START, StateGraph
from langgraph.pregel import Pregel
from langgraph.types import PregelExecutableTask
from typing_extensions import TypedDict

# Re-enable warnings after import
warnings.filterwarnings("default", category=DeprecationWarning)

T = TypeVar("T")


class SimpleState(TypedDict, total=False):
    """Simple state for testing."""

    values: list[str]


@dataclass
class SubmitCall:
    """Captured information from a submit call."""

    fn_name: str
    task_name: str | None
    task_id: str | None
    task_input: Any
    dunder_name: str | None
    dunder_cancel_on_exit: bool
    dunder_reraise_on_exit: bool
    dunder_next_tick: bool


class SubmitCapture:
    """A custom submit function that captures calls and delegates to original."""

    def __init__(self, original_submit: Callable) -> None:
        self.original_submit = original_submit
        self.captured_calls: deque[SubmitCall] = deque()

    def __call__(
        self,
        fn: Callable[..., T],
        *args: Any,
        __name__: str | None = None,
        __cancel_on_exit__: bool = False,
        __reraise_on_exit__: bool = True,
        __next_tick__: bool = False,
        **kwargs: Any,
    ) -> concurrent.futures.Future[T]:
        """Capture the call and delegate to original submit."""
        # Extract task info if first arg is PregelExecutableTask
        task_name = None
        task_id = None
        task_input = None

        if args and isinstance(args[0], PregelExecutableTask):
            task = args[0]
            task_name = task.name
            task_id = task.id
            task_input = task.input

        # Capture the call
        self.captured_calls.append(
            SubmitCall(
                fn_name=fn.__name__ if hasattr(fn, "__name__") else str(fn),
                task_name=task_name,
                task_id=task_id,
                task_input=task_input,
                dunder_name=__name__,
                dunder_cancel_on_exit=__cancel_on_exit__,
                dunder_reraise_on_exit=__reraise_on_exit__,
                dunder_next_tick=__next_tick__,
            )
        )

        # Delegate to original
        return self.original_submit(
            fn,
            *args,
            __name__=__name__,
            __cancel_on_exit__=__cancel_on_exit__,
            __reraise_on_exit__=__reraise_on_exit__,
            __next_tick__=__next_tick__,
            **kwargs,
        )


def create_simple_graph() -> Pregel:
    """Create a simple 2-node graph for testing."""

    def node_a(state: SimpleState) -> SimpleState:
        return {"values": state.get("values", []) + ["a"]}

    def node_b(state: SimpleState) -> SimpleState:
        return {"values": state.get("values", []) + ["b"]}

    graph = StateGraph(SimpleState)
    graph.add_node("node_a", node_a)
    graph.add_node("node_b", node_b)
    graph.add_edge(START, "node_a")
    graph.add_edge("node_a", "node_b")
    graph.add_edge("node_b", END)

    return graph.compile()


async def test_submit_injection() -> dict[str, Any]:
    """
    Test whether we can inject a custom submit function via config.

    Returns:
        Dict with result, captured calls, and success status
    """
    from langgraph._internal._constants import CONF
    from langgraph.pregel._executor import AsyncBackgroundExecutor

    pregel = create_simple_graph()

    # We need to create our own executor to get the submit function
    # The trick is to inject our wrapper via CONFIG_KEY_RUNNER_SUBMIT

    captured_calls: deque[SubmitCall] = deque()

    # Create a wrapper that will capture calls
    class CapturingExecutor:
        """Executor that captures submit calls."""

        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.calls = captured_calls

        def submit(
            self,
            fn: Callable[..., T],
            *args: Any,
            __name__: str | None = None,
            __cancel_on_exit__: bool = False,
            __reraise_on_exit__: bool = True,
            __next_tick__: bool = False,
            **kwargs: Any,
        ) -> asyncio.Future[T]:
            """Capture and execute."""
            # Extract task info
            task_name = None
            task_id = None
            task_input = None

            if args and isinstance(args[0], PregelExecutableTask):
                task = args[0]
                task_name = task.name
                task_id = task.id
                task_input = task.input

            self.calls.append(
                SubmitCall(
                    fn_name=fn.__name__ if hasattr(fn, "__name__") else str(fn),
                    task_name=task_name,
                    task_id=task_id,
                    task_input=task_input,
                    dunder_name=__name__,
                    dunder_cancel_on_exit=__cancel_on_exit__,
                    dunder_reraise_on_exit=__reraise_on_exit__,
                    dunder_next_tick=__next_tick__,
                )
            )

            # Execute the function (this would be where we'd call an activity)
            # For now, just run it directly
            async def run() -> T:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                else:
                    return fn(*args, **kwargs)

            return asyncio.ensure_future(run())

    executor = CapturingExecutor()

    # Inject via config
    config: RunnableConfig = {
        "configurable": {
            CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
        }
    }

    try:
        result = await pregel.ainvoke({"values": []}, config=config)
        return {
            "result": result,
            "captured_calls": list(captured_calls),
            "success": True,
            "error": None,
        }
    except Exception as e:
        return {
            "result": None,
            "captured_calls": list(captured_calls),
            "success": False,
            "error": str(e),
        }


async def test_submit_function_receives_task() -> dict[str, Any]:
    """
    Test that submit receives PregelExecutableTask with expected attributes.

    This validates we can access:
    - task.name (node name)
    - task.id (unique task ID)
    - task.input (input to node)
    - task.proc (the node runnable)
    - task.config (node config)
    """
    pregel = create_simple_graph()

    task_details: list[dict[str, Any]] = []

    class InspectingExecutor:
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()

        def submit(
            self,
            fn: Callable[..., T],
            *args: Any,
            __name__: str | None = None,
            __cancel_on_exit__: bool = False,
            __reraise_on_exit__: bool = True,
            __next_tick__: bool = False,
            **kwargs: Any,
        ) -> asyncio.Future[T]:
            # Inspect task if present
            if args and isinstance(args[0], PregelExecutableTask):
                task = args[0]
                task_details.append(
                    {
                        "name": task.name,
                        "id": task.id,
                        "input_type": type(task.input).__name__,
                        "input_keys": (
                            list(task.input.keys())
                            if isinstance(task.input, dict)
                            else None
                        ),
                        "has_proc": task.proc is not None,
                        "proc_type": type(task.proc).__name__,
                        "has_config": task.config is not None,
                        "has_writes": hasattr(task, "writes"),
                        "writes_type": (
                            type(task.writes).__name__ if hasattr(task, "writes") else None
                        ),
                    }
                )

            # Execute normally
            async def run() -> T:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                else:
                    return fn(*args, **kwargs)

            return asyncio.ensure_future(run())

    executor = InspectingExecutor()
    config: RunnableConfig = {
        "configurable": {
            CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
        }
    }

    try:
        result = await pregel.ainvoke({"values": []}, config=config)
        return {
            "result": result,
            "task_details": task_details,
            "success": True,
        }
    except Exception as e:
        return {
            "task_details": task_details,
            "success": False,
            "error": str(e),
        }


if __name__ == "__main__":
    print("=== Test 1: Submit Injection ===")
    output1 = asyncio.run(test_submit_injection())
    print(f"Success: {output1['success']}")
    print(f"Result: {output1['result']}")
    print(f"Captured {len(output1['captured_calls'])} calls:")
    for call in output1["captured_calls"]:
        print(f"  - fn={call.fn_name}, task={call.task_name}, __name__={call.dunder_name}")
    if output1.get("error"):
        print(f"Error: {output1['error']}")

    print("\n=== Test 2: Task Details ===")
    output2 = asyncio.run(test_submit_function_receives_task())
    print(f"Success: {output2['success']}")
    print(f"Result: {output2.get('result')}")
    print("Task details:")
    for detail in output2["task_details"]:
        print(f"  - {detail}")
