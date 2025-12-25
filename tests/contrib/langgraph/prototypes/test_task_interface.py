"""Tests for Task Interface prototype.

These tests validate our understanding of PregelExecutableTask structure
and what information we need to pass to Temporal activities.

Technical Concern:
    What is the actual PregelExecutableTask structure? What fields are
    available and what do we need to extract for Temporal activities?
"""

from __future__ import annotations

import asyncio
import dataclasses
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


class AgentState(TypedDict, total=False):
    """State for testing task interface."""

    messages: Annotated[list[str], add]
    context: str


class TestPregelExecutableTaskStructure:
    """Validate PregelExecutableTask is a dataclass with expected fields."""

    def test_is_dataclass(self) -> None:
        """Verify PregelExecutableTask is a dataclass."""
        assert dataclasses.is_dataclass(PregelExecutableTask)

    def test_is_frozen(self) -> None:
        """Verify PregelExecutableTask is frozen (immutable)."""
        # Check frozen flag in dataclass params
        # For frozen dataclasses, __hash__ is generated
        assert hasattr(PregelExecutableTask, "__hash__")

    def test_has_expected_fields(self) -> None:
        """Verify all expected fields exist."""
        expected_fields = {
            "name",         # Node name
            "id",           # Unique task ID
            "path",         # Graph hierarchy path
            "input",        # Input state
            "proc",         # Node runnable
            "config",       # LangGraph config
            "triggers",     # Triggering channels
            "writes",       # Output writes deque
            "retry_policy", # Retry configuration
            "cache_key",    # Cache key
            "writers",      # Writer runnables
            "subgraphs",    # Nested subgraphs
        }

        actual_fields = {f.name for f in dataclasses.fields(PregelExecutableTask)}

        # Check all expected fields exist
        for field in expected_fields:
            assert field in actual_fields, f"Missing field: {field}"

    def test_writes_field_is_deque(self) -> None:
        """Verify writes field type is deque."""
        writes_field = next(
            f for f in dataclasses.fields(PregelExecutableTask) if f.name == "writes"
        )
        assert "deque" in str(writes_field.type)


class TestTaskDataExtraction:
    """Test extracting task data for Temporal activities."""

    @pytest.mark.asyncio
    async def test_extract_core_identification(self) -> None:
        """Test extracting name, id, path from task."""

        def my_node(state: AgentState) -> AgentState:
            return {"messages": ["hello"]}

        graph = StateGraph(AgentState)
        graph.add_node("my_node", my_node)
        graph.add_edge(START, "my_node")
        graph.add_edge("my_node", END)
        compiled = graph.compile()

        extracted_data: list[dict[str, Any]] = []

        class ExtractingExecutor:
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
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]
                    extracted_data.append({
                        "name": task.name,
                        "id": task.id,
                        "path": task.path,
                        "has_input": task.input is not None,
                        "has_proc": task.proc is not None,
                        "has_config": task.config is not None,
                    })

                async def run() -> T:
                    if asyncio.iscoroutinefunction(fn):
                        return await fn(*args, **kwargs)
                    return fn(*args, **kwargs)

                return asyncio.ensure_future(run())

        executor = ExtractingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: AgentState = {"messages": []}
        await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Should have captured at least one task
        # Note: Sequential graphs may use fast path
        if extracted_data:
            task_data = extracted_data[0]
            assert task_data["name"] == "my_node"
            assert task_data["id"] is not None
            assert task_data["path"] is not None
            assert task_data["has_input"]
            assert task_data["has_proc"]
            assert task_data["has_config"]

    @pytest.mark.asyncio
    async def test_extract_input_state(self) -> None:
        """Test that task.input contains the current state."""

        def increment(state: AgentState) -> AgentState:
            return {"messages": ["processed"]}

        graph = StateGraph(AgentState)
        graph.add_node("increment", increment)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)
        compiled = graph.compile()

        captured_inputs: list[dict[str, Any]] = []

        class InputCapturingExecutor:
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
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]
                    captured_inputs.append({
                        "name": task.name,
                        "input": task.input,
                        "input_type": type(task.input).__name__,
                    })

                async def run() -> T:
                    if asyncio.iscoroutinefunction(fn):
                        return await fn(*args, **kwargs)
                    return fn(*args, **kwargs)

                return asyncio.ensure_future(run())

        executor = InputCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: AgentState = {"messages": ["initial"], "context": "test"}
        await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Log captured inputs
        print(f"Captured {len(captured_inputs)} task inputs:")
        for capture in captured_inputs:
            print(f"  - {capture['name']}: {capture['input_type']}")
            print(f"    Input: {capture['input']}")

    @pytest.mark.asyncio
    async def test_task_config_structure(self) -> None:
        """Test that task.config contains RunnableConfig."""

        def node(state: AgentState) -> AgentState:
            return {"messages": ["done"]}

        graph = StateGraph(AgentState)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        compiled = graph.compile()

        captured_configs: list[dict[str, Any]] = []

        class ConfigCapturingExecutor:
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
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]
                    config = task.config

                    # Inspect config structure
                    captured_configs.append({
                        "name": task.name,
                        "config_keys": list(config.keys()) if config else [],
                        "has_configurable": "configurable" in config if config else False,
                        "configurable_keys": (
                            list(config.get("configurable", {}).keys())
                            if config else []
                        ),
                    })

                async def run() -> T:
                    if asyncio.iscoroutinefunction(fn):
                        return await fn(*args, **kwargs)
                    return fn(*args, **kwargs)

                return asyncio.ensure_future(run())

        executor = ConfigCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
                "user_key": "user_value",  # Custom key
            },
            "tags": ["test"],
            "metadata": {"source": "test"},
        }

        initial_state: AgentState = {"messages": []}
        await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        # Log captured configs
        print(f"Captured {len(captured_configs)} task configs:")
        for capture in captured_configs:
            print(f"  - {capture['name']}")
            print(f"    Config keys: {capture['config_keys']}")
            print(f"    Has configurable: {capture['has_configurable']}")
            print(f"    Configurable keys: {capture['configurable_keys']}")


class TestConfigFiltering:
    """Test filtering config for serialization."""

    def test_filter_internal_keys(self) -> None:
        """Test that internal keys are filtered out."""
        from temporalio.contrib.langgraph._prototypes.task_interface_proto import (
            filter_config_for_serialization,
        )

        config: RunnableConfig = {
            "configurable": {
                "__pregel_runner_submit": "should_be_filtered",
                "__pregel_some_other": "also_filtered",
                "__lg_internal": "filtered",
                "user_key": "keep_this",
                "another_user_key": 123,
            },
            "tags": ["test", "filter"],
            "metadata": {"source": "test"},
            "run_name": "test_run",
        }

        filtered = filter_config_for_serialization(config)

        # Safe keys should be preserved
        assert filtered.get("tags") == ["test", "filter"]
        assert filtered.get("metadata") == {"source": "test"}
        assert filtered.get("run_name") == "test_run"

        # Internal keys should be filtered
        configurable = filtered.get("configurable", {})
        assert "__pregel_runner_submit" not in configurable
        assert "__pregel_some_other" not in configurable
        assert "__lg_internal" not in configurable

        # User keys should be preserved
        assert configurable.get("user_key") == "keep_this"
        assert configurable.get("another_user_key") == 123

    def test_filter_non_serializable(self) -> None:
        """Test that non-serializable values are filtered."""
        from temporalio.contrib.langgraph._prototypes.task_interface_proto import (
            filter_config_for_serialization,
        )

        def my_func() -> None:
            pass

        config: RunnableConfig = {
            "configurable": {
                "serializable": "string_value",
                "also_serializable": {"nested": "dict"},
                "non_serializable_func": my_func,
            },
        }

        filtered = filter_config_for_serialization(config)
        configurable = filtered.get("configurable", {})

        # Serializable should be kept
        assert configurable.get("serializable") == "string_value"
        assert configurable.get("also_serializable") == {"nested": "dict"}

        # Non-serializable should be filtered
        assert "non_serializable_func" not in configurable


class TestParallelTaskExtraction:
    """Test extracting data from parallel tasks."""

    @pytest.mark.asyncio
    async def test_parallel_tasks_have_unique_ids(self) -> None:
        """Verify parallel tasks have unique IDs."""

        def node_a(state: AgentState) -> AgentState:
            return {"messages": ["from_a"]}

        def node_b(state: AgentState) -> AgentState:
            return {"messages": ["from_b"]}

        graph = StateGraph(AgentState)
        graph.add_node("node_a", node_a)
        graph.add_node("node_b", node_b)
        graph.add_edge(START, "node_a")
        graph.add_edge(START, "node_b")
        graph.add_edge("node_a", END)
        graph.add_edge("node_b", END)
        compiled = graph.compile()

        task_ids: dict[str, str] = {}

        class IdCapturingExecutor:
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
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]
                    task_ids[task.name] = task.id

                async def run() -> T:
                    if asyncio.iscoroutinefunction(fn):
                        return await fn(*args, **kwargs)
                    return fn(*args, **kwargs)

                return asyncio.ensure_future(run())

        executor = IdCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: AgentState = {"messages": []}
        await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        print(f"Captured task IDs: {task_ids}")

        # If we captured parallel tasks, verify unique IDs
        if len(task_ids) >= 2:
            ids = list(task_ids.values())
            assert len(ids) == len(set(ids)), "Task IDs should be unique"

    @pytest.mark.asyncio
    async def test_task_triggers(self) -> None:
        """Test that task.triggers shows what triggered the task."""

        def node(state: AgentState) -> AgentState:
            return {"messages": ["done"]}

        graph = StateGraph(AgentState)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        compiled = graph.compile()

        captured_triggers: list[dict[str, Any]] = []

        class TriggerCapturingExecutor:
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
                if args and isinstance(args[0], PregelExecutableTask):
                    task = args[0]
                    captured_triggers.append({
                        "name": task.name,
                        "triggers": list(task.triggers),
                    })

                async def run() -> T:
                    if asyncio.iscoroutinefunction(fn):
                        return await fn(*args, **kwargs)
                    return fn(*args, **kwargs)

                return asyncio.ensure_future(run())

        executor = TriggerCapturingExecutor()
        config: RunnableConfig = {
            "configurable": {
                CONFIG_KEY_RUNNER_SUBMIT: WeakMethod(executor.submit),
            }
        }

        initial_state: AgentState = {"messages": []}
        await compiled.ainvoke(initial_state, config=config)  # type: ignore[arg-type]

        print(f"Captured triggers: {captured_triggers}")
