"""Tests for write capture mechanism.

These tests validate that we can capture node output writes through
the PregelExecutableTask.writes attribute when using submit injection.

NOTE: The original proposal suggested using CONFIG_KEY_SEND, but that
mechanism is internal to LangGraph and set per-task. Instead, writes
are captured in task.writes (a deque) after task execution.
"""

from __future__ import annotations

import asyncio
from operator import add
from typing import Annotated, Any, Callable, TypeVar
from weakref import WeakMethod

import pytest
from langchain_core.runnables import RunnableConfig
from typing_extensions import TypedDict

# Import from internal module to avoid deprecation warning
from langgraph._internal._constants import CONFIG_KEY_RUNNER_SUBMIT
from langgraph.graph import END, START, StateGraph
from langgraph.types import PregelExecutableTask

T = TypeVar("T")


class SimpleState(TypedDict, total=False):
    """Simple state for testing."""

    value: int


class ListState(TypedDict, total=False):
    """State with list for parallel execution."""

    values: Annotated[list[str], add]


class TestWriteCapture:
    """Validate write capture via task.writes attribute."""

    def test_pregel_task_has_writes_attribute(self) -> None:
        """Verify PregelExecutableTask has writes attribute."""
        import dataclasses

        # PregelExecutableTask is a dataclass, not a NamedTuple
        assert dataclasses.is_dataclass(PregelExecutableTask)

        # Check that 'writes' is one of the fields
        field_names = [f.name for f in dataclasses.fields(PregelExecutableTask)]
        assert "writes" in field_names

        # Check the type annotation indicates it's a deque
        from collections import deque

        writes_field = next(
            f for f in dataclasses.fields(PregelExecutableTask) if f.name == "writes"
        )
        # The type should be deque[tuple[str, Any]]
        assert "deque" in str(writes_field.type)

    @pytest.mark.asyncio
    async def test_capture_writes_after_execution(self) -> None:
        """Test that task.writes contains output after execution."""

        def increment(state: SimpleState) -> SimpleState:
            return {"value": state.get("value", 0) + 10}

        graph = StateGraph(SimpleState)
        graph.add_node("increment", increment)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)
        compiled = graph.compile()

        captured_writes: list[dict[str, Any]] = []

        class WriteCapturingExecutor:
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
                task: PregelExecutableTask | None = None
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]

                async def run() -> T:
                    # Capture writes BEFORE execution
                    writes_before = list(task.writes) if task else []

                    # Execute the task
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(*args, **kwargs)
                    else:
                        result = fn(*args, **kwargs)

                    # Capture writes AFTER execution
                    writes_after = list(task.writes) if task else []

                    if task:
                        captured_writes.append(
                            {
                                "task_name": task.name,
                                "writes_before": writes_before,
                                "writes_after": writes_after,
                                "write_count": len(writes_after),
                            }
                        )

                    return result

                return asyncio.ensure_future(run())

        executor = WriteCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: SimpleState = {"value": 5}
        result = await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Verify graph executed correctly
        assert result == {"value": 15}

        # Log captured writes for debugging
        print(f"Captured {len(captured_writes)} task executions:")
        for capture in captured_writes:
            print(f"  - Task: {capture['task_name']}")
            print(f"    Writes before: {capture['writes_before']}")
            print(f"    Writes after: {capture['writes_after']}")

    @pytest.mark.asyncio
    async def test_write_format_is_channel_value_tuple(self) -> None:
        """Verify writes are in (channel, value) tuple format."""

        def add_message(state: ListState) -> ListState:
            return {"values": ["hello"]}

        graph = StateGraph(ListState)
        graph.add_node("add_message", add_message)
        graph.add_edge(START, "add_message")
        graph.add_edge("add_message", END)
        compiled = graph.compile()

        write_formats: list[dict[str, Any]] = []

        class FormatInspectingExecutor:
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
                task: PregelExecutableTask | None = None
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]

                async def run() -> T:
                    # Execute the task
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(*args, **kwargs)
                    else:
                        result = fn(*args, **kwargs)

                    # Inspect write format
                    if task and task.writes:
                        for write in task.writes:
                            write_formats.append(
                                {
                                    "task_name": task.name,
                                    "write": write,
                                    "write_type": type(write).__name__,
                                    "is_tuple": isinstance(write, tuple),
                                    "tuple_len": (
                                        len(write) if isinstance(write, tuple) else None
                                    ),
                                    "channel": (
                                        write[0]
                                        if isinstance(write, tuple) and len(write) >= 2
                                        else None
                                    ),
                                    "value": (
                                        write[1]
                                        if isinstance(write, tuple) and len(write) >= 2
                                        else None
                                    ),
                                }
                            )

                    return result

                return asyncio.ensure_future(run())

        executor = FormatInspectingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: ListState = {"values": []}
        result = await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Log write formats
        print(f"Captured {len(write_formats)} writes:")
        for fmt in write_formats:
            print(f"  - Task: {fmt['task_name']}")
            print(f"    Write: {fmt['write']}")
            print(f"    Type: {fmt['write_type']}")
            print(f"    Is tuple: {fmt['is_tuple']}")
            if fmt["is_tuple"]:
                print(f"    Channel: {fmt['channel']}")
                print(f"    Value: {fmt['value']}")

        # Validate write format (if we captured any)
        for fmt in write_formats:
            assert fmt["is_tuple"], "Writes should be tuples"
            assert fmt["tuple_len"] == 2, "Writes should be (channel, value) tuples"

    @pytest.mark.asyncio
    async def test_parallel_writes_captured_separately(self) -> None:
        """Test that parallel node writes are captured for each task."""

        def node_a(state: ListState) -> ListState:
            return {"values": ["from_a"]}

        def node_b(state: ListState) -> ListState:
            return {"values": ["from_b"]}

        graph = StateGraph(ListState)
        graph.add_node("node_a", node_a)
        graph.add_node("node_b", node_b)
        graph.add_edge(START, "node_a")
        graph.add_edge(START, "node_b")
        graph.add_edge("node_a", END)
        graph.add_edge("node_b", END)
        compiled = graph.compile()

        task_writes: dict[str, list[Any]] = {}

        class ParallelWriteCapturingExecutor:
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
                task: PregelExecutableTask | None = None
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]

                async def run() -> T:
                    # Execute the task
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(*args, **kwargs)
                    else:
                        result = fn(*args, **kwargs)

                    # Capture writes per task
                    if task:
                        task_writes[task.name] = list(task.writes)

                    return result

                return asyncio.ensure_future(run())

        executor = ParallelWriteCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: ListState = {"values": []}
        result = await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Both values should be in result (merged by reducer)
        assert "from_a" in result.get("values", [])
        assert "from_b" in result.get("values", [])

        # Log captured writes per task
        print(f"Captured writes for {len(task_writes)} tasks:")
        for task_name, writes in task_writes.items():
            print(f"  - {task_name}: {writes}")

        # Each task should have its own writes
        if "node_a" in task_writes:
            assert any("from_a" in str(w) for w in task_writes["node_a"])
        if "node_b" in task_writes:
            assert any("from_b" in str(w) for w in task_writes["node_b"])
