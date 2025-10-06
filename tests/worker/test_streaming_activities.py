"""
Comprehensive test suite for streaming activities functionality.

Tests streaming activities with both peekable and non-peekable modes,
error handling, cancellation, and integration with existing workflow patterns.
"""

import asyncio
import multiprocessing
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterable, Callable, List, Optional

import temporalio.activity
import temporalio.common
import temporalio.worker
import temporalio.workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker, SharedStateManager
from tests.helpers.worker import (
    ExternalWorker,
)

# Passing through because Python 3.9 has an import bug at
# https://github.com/python/cpython/issues/91351
with temporalio.workflow.unsafe.imports_passed_through():
    import pytest


_default_shared_state_manager = SharedStateManager.create_from_multiprocessing(
    multiprocessing.Manager()
)


# Test Activities
@temporalio.activity.defn
async def streaming_activity_10_items() -> AsyncIterable[int]:
    """Streaming activity that yields 10 integers."""
    for i in range(10):
        await asyncio.sleep(0.01)  # Small delay to simulate work
        temporalio.activity.heartbeat(f"Processing item {i}")
        yield i


@temporalio.activity.defn
async def streaming_activity_large() -> AsyncIterable[int]:
    """Streaming activity that yields 100 integers for performance testing."""
    for i in range(100):
        if i % 10 == 0:
            await asyncio.sleep(0.001)  # Occasional delay
            temporalio.activity.heartbeat(f"Batch {i//10}")
        yield i


@temporalio.activity.defn
async def empty_streaming_activity() -> AsyncIterable[int]:
    """Streaming activity that yields no items."""
    return
    yield  # Never reached, but needed for type checker


@temporalio.activity.defn
async def streaming_activity_with_error() -> AsyncIterable[int]:
    """Streaming activity that yields some items then raises an error."""
    for i in range(3):
        yield i
    raise RuntimeError("Activity failed after yielding items")


@temporalio.activity.defn
async def regular_activity() -> List[int]:
    """Regular non-streaming activity that returns a list."""
    await asyncio.sleep(0.01)
    return [1, 2, 3, 4, 5]


@dataclass
class _StreamingActivityResult:
    """Result container for streaming activity tests."""
    act_task_queue: str
    result: Any
    handle: WorkflowHandle


# Test workflows (must be at module level, not local)
@temporalio.workflow.defn
class StreamingPeekableWorkflow:
    @temporalio.workflow.run
    async def run(self, activity_fn_name: str, *args: Any) -> List[Any]:
        # Get the activity function by name from globals
        activity_fn = globals()[activity_fn_name]
        results = []
        async for item in await temporalio.workflow.execute_local_activity(
            activity_fn,
            *args,
            schedule_to_close_timeout=timedelta(seconds=10),
            peekable=True
        ):
            results.append(item)
        return results


@temporalio.workflow.defn
class StreamingBufferedWorkflow:
    @temporalio.workflow.run
    async def run(self, activity_fn_name: str, *args: Any) -> List[Any]:
        # Get the activity function by name from globals
        activity_fn = globals()[activity_fn_name]
        iterable_result = await temporalio.workflow.execute_local_activity(
            activity_fn,
            *args,
            schedule_to_close_timeout=timedelta(seconds=10),
            peekable=False
        )
        results = []
        async for item in iterable_result:
            results.append(item)
        return results


@temporalio.workflow.defn
class RegularActivityWorkflow:
    @temporalio.workflow.run
    async def run(self) -> List[int]:
        return await temporalio.workflow.execute_local_activity(
            regular_activity,
            schedule_to_close_timeout=timedelta(seconds=10),
            peekable=False
        )


@temporalio.workflow.defn
class PeekModeEnforcementWorkflow:
    @temporalio.workflow.run
    async def run(self) -> str:
        # Start first peekable activity
        task1 = asyncio.create_task(self._consume_streaming_activity("first"))
        
        # Try to start second peekable activity (should fail)
        try:
            task2 = asyncio.create_task(self._consume_streaming_activity("second"))
            await task2
            return "ERROR: Second peekable activity should have failed"
        except RuntimeError as e:
            if "Only one peekable activity is allowed" in str(e):
                # Expected error - wait for first task to complete
                result1 = await task1
                return f"SUCCESS: {result1}"
            else:
                return f"ERROR: Unexpected error: {e}"

    async def _consume_streaming_activity(self, name: str) -> str:
        results = []
        async for item in await temporalio.workflow.execute_local_activity(
            streaming_activity_10_items,
            schedule_to_close_timeout=timedelta(seconds=10),
            peekable=True
        ):
            results.append(item)
        return f"{name}_completed_with_{len(results)}_items"


async def _execute_streaming_workflow_peekable(
    client: Client,
    worker: ExternalWorker,
    activity_fn: Callable,
    *args: Any,
    schedule_to_close_timeout_ms: int = 10000,
    additional_activities: List[Callable] = [],
) -> _StreamingActivityResult:
    """Execute streaming activity with peekable=True using global workflow class."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [activity_fn] + additional_activities,
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    async with Worker(**worker_config, workflows=[StreamingPeekableWorkflow]):
        handle = await client.start_workflow(
            StreamingPeekableWorkflow.run,
            activity_fn.__name__,
            *args,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        return _StreamingActivityResult(
            act_task_queue=worker_config["task_queue"],
            result=await handle.result(),
            handle=handle,
        )


async def _execute_streaming_workflow_buffered(
    client: Client,
    worker: ExternalWorker,
    activity_fn: Callable,
    *args: Any,
    schedule_to_close_timeout_ms: int = 10000,
    additional_activities: List[Callable] = [],
) -> _StreamingActivityResult:
    """Execute streaming activity with peekable=False using global workflow class."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [activity_fn] + additional_activities,
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    async with Worker(**worker_config, workflows=[StreamingBufferedWorkflow]):
        handle = await client.start_workflow(
            StreamingBufferedWorkflow.run,
            activity_fn.__name__,
            *args,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        return _StreamingActivityResult(
            act_task_queue=worker_config["task_queue"],
            result=await handle.result(),
            handle=handle,
        )


def assert_streaming_activity_error(err: WorkflowFailureError) -> BaseException:
    """Assert workflow failed due to streaming activity error and return the cause."""
    assert isinstance(err.cause, ActivityError)
    assert err.cause.__cause__
    return err.cause.__cause__


def assert_streaming_activity_application_error(
    err: WorkflowFailureError,
) -> ApplicationError:
    """Assert workflow failed due to streaming activity application error."""
    ret = assert_streaming_activity_error(err)
    assert isinstance(ret, ApplicationError)
    return ret


# Core functionality tests
async def test_streaming_activity_detection():
    """Test that AsyncIterable return types are detected as streaming."""
    # Test streaming activity detection
    streaming_defn = temporalio.activity._Definition.must_from_callable(streaming_activity_10_items)
    assert streaming_defn.is_streaming == True, "Streaming activity should be detected"
    
    # Test regular activity detection  
    regular_defn = temporalio.activity._Definition.must_from_callable(regular_activity)
    assert regular_defn.is_streaming == False, "Regular activity should not be streaming"


async def test_streaming_peekable_basic(client: Client, worker: ExternalWorker):
    """Test basic streaming activity with peekable=True."""
    result = await _execute_streaming_workflow_peekable(
        client, worker, streaming_activity_10_items
    )
    assert result.result == list(range(10)), f"Expected [0,1,2...9], got {result.result}"


async def test_streaming_non_peekable_basic(client: Client, worker: ExternalWorker):
    """Test basic streaming activity with peekable=False."""
    result = await _execute_streaming_workflow_buffered(
        client, worker, streaming_activity_10_items
    )
    assert result.result == list(range(10)), f"Expected [0,1,2...9], got {result.result}"


async def test_empty_streaming_peekable(client: Client, worker: ExternalWorker):
    """Test streaming activity that yields no items with peekable=True."""
    result = await _execute_streaming_workflow_peekable(
        client, worker, empty_streaming_activity
    )
    assert result.result == [], f"Expected empty list, got {result.result}"


async def test_empty_streaming_non_peekable(client: Client, worker: ExternalWorker):
    """Test streaming activity that yields no items with peekable=False."""
    result = await _execute_streaming_workflow_buffered(
        client, worker, empty_streaming_activity
    )
    assert result.result == [], f"Expected empty list, got {result.result}"


async def test_large_streaming_peekable(client: Client, worker: ExternalWorker):
    """Test large streaming activity (100 items) with peekable=True."""
    result = await _execute_streaming_workflow_peekable(
        client, worker, streaming_activity_large, schedule_to_close_timeout_ms=30000
    )
    assert result.result == list(range(100)), f"Expected [0,1,2...99], got length {len(result.result)}"


async def test_streaming_error_handling_peekable(client: Client, worker: ExternalWorker):
    """Test error handling in streaming activity with peekable=True."""
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_peekable(
            client, worker, streaming_activity_with_error
        )
    
    error = assert_streaming_activity_application_error(exc_info.value)
    assert "Activity failed after yielding items" in str(error)


async def test_streaming_error_handling_non_peekable(client: Client, worker: ExternalWorker):
    """Test error handling in streaming activity with peekable=False."""
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_buffered(
            client, worker, streaming_activity_with_error
        )
    
    error = assert_streaming_activity_application_error(exc_info.value)
    assert "Activity failed after yielding items" in str(error)


async def test_regular_activity_unchanged(client: Client, worker: ExternalWorker):
    """Test that regular activities work unchanged."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [regular_activity],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    async with Worker(**worker_config, workflows=[RegularActivityWorkflow]):
        handle = await client.start_workflow(
            RegularActivityWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        assert result == [1, 2, 3, 4, 5], f"Expected [1,2,3,4,5], got {result}"


async def test_peek_mode_enforcement(client: Client, worker: ExternalWorker):
    """Test that only one peekable activity is allowed at a time."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [streaming_activity_10_items],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    async with Worker(**worker_config, workflows=[PeekModeEnforcementWorkflow]):
        handle = await client.start_workflow(
            PeekModeEnforcementWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        assert "SUCCESS:" in result, f"Expected success message, got {result}"
        assert "first_completed_with_10_items" in result, f"Expected first activity completion in {result}"


# Advanced test activities
@temporalio.activity.defn
async def streaming_activity_with_heartbeat() -> AsyncIterable[int]:
    """Streaming activity with heartbeat testing."""
    for i in range(5):
        await asyncio.sleep(0.02)  # Slightly longer delay
        temporalio.activity.heartbeat({"progress": i, "message": f"Processing item {i}"})
        yield i


@temporalio.activity.defn
async def streaming_activity_cancellation_test() -> AsyncIterable[int]:
    """Streaming activity for testing cancellation behavior."""
    for i in range(20):  # Long-running to allow cancellation
        if temporalio.activity.is_cancelled():
            break
        await asyncio.sleep(0.01)
        temporalio.activity.heartbeat(f"Item {i}")
        yield i


@temporalio.activity.defn
async def streaming_activity_mixed_types() -> AsyncIterable[str]:
    """Streaming activity yielding different string values."""
    values = ["hello", "world", "streaming", "activity", "test"]
    for value in values:
        await asyncio.sleep(0.01)
        temporalio.activity.heartbeat(f"Yielding: {value}")
        yield value


@temporalio.activity.defn
async def streaming_activity_error_midway() -> AsyncIterable[int]:
    """Streaming activity that fails after yielding half its items."""
    for i in range(5):
        yield i
        if i == 2:  # Fail after yielding items 0, 1, 2
            raise ValueError(f"Planned error at item {i}")


@temporalio.activity.defn  
async def regular_activity_with_args(name: str, count: int) -> List[str]:
    """Regular activity with arguments for testing compatibility."""
    await asyncio.sleep(0.01)
    return [f"{name}_{i}" for i in range(count)]


# Performance and stress tests
async def test_streaming_activity_performance_peekable(client: Client, worker: ExternalWorker):
    """Test performance of large streaming activity with peekable=True."""
    result = await _execute_streaming_workflow_peekable(
        client, worker, streaming_activity_large, schedule_to_close_timeout_ms=60000
    )
    assert len(result.result) == 100
    assert result.result[0] == 0
    assert result.result[99] == 99


async def test_streaming_activity_performance_buffered(client: Client, worker: ExternalWorker):
    """Test performance of large streaming activity with peekable=False."""
    result = await _execute_streaming_workflow_buffered(
        client, worker, streaming_activity_large, schedule_to_close_timeout_ms=60000
    )
    assert len(result.result) == 100
    assert result.result[0] == 0
    assert result.result[99] == 99


async def test_streaming_activity_heartbeat(client: Client, worker: ExternalWorker):
    """Test streaming activity with heartbeats."""
    result = await _execute_streaming_workflow_peekable(
        client, worker, streaming_activity_with_heartbeat
    )
    assert result.result == [0, 1, 2, 3, 4]


async def test_streaming_activity_mixed_types(client: Client, worker: ExternalWorker):
    """Test streaming activity with non-integer return types."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [streaming_activity_mixed_types],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    @temporalio.workflow.defn
    class StringStreamingWorkflow:
        @temporalio.workflow.run
        async def run(self) -> List[str]:
            results = []
            async for item in await temporalio.workflow.execute_local_activity(
                streaming_activity_mixed_types,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=True
            ):
                results.append(item)
            return results
    
    async with Worker(**worker_config, workflows=[StringStreamingWorkflow]):
        handle = await client.start_workflow(
            StringStreamingWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        expected = ["hello", "world", "streaming", "activity", "test"]
        assert result == expected, f"Expected {expected}, got {result}"


# Error handling and edge cases
async def test_streaming_activity_error_midway_peekable(client: Client, worker: ExternalWorker):
    """Test error handling when streaming activity fails partway through (peekable)."""
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_peekable(
            client, worker, streaming_activity_error_midway
        )
    
    error = assert_streaming_activity_application_error(exc_info.value)
    assert "Planned error at item 2" in str(error)


async def test_streaming_activity_error_midway_buffered(client: Client, worker: ExternalWorker):
    """Test error handling when streaming activity fails partway through (buffered)."""
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_buffered(
            client, worker, streaming_activity_error_midway
        )
    
    error = assert_streaming_activity_application_error(exc_info.value)
    assert "Planned error at item 2" in str(error)


async def test_regular_activity_with_peekable_false(client: Client, worker: ExternalWorker):
    """Test that regular activities work with peekable=False (should be ignored)."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [regular_activity_with_args],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    @temporalio.workflow.defn
    class RegularActivityPeekableTestWorkflow:
        @temporalio.workflow.run
        async def run(self) -> List[str]:
            # peekable should be ignored for regular activities
            return await temporalio.workflow.execute_local_activity(
                regular_activity_with_args,
                "test",
                3,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=False  # This should be ignored since it's not a streaming activity
            )
    
    async with Worker(**worker_config, workflows=[RegularActivityPeekableTestWorkflow]):
        handle = await client.start_workflow(
            RegularActivityPeekableTestWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        expected = ["test_0", "test_1", "test_2"]
        assert result == expected, f"Expected {expected}, got {result}"


async def test_streaming_activity_immediate_error(client: Client, worker: ExternalWorker):
    """Test streaming activity that fails immediately before yielding any items."""
    @temporalio.activity.defn
    async def streaming_activity_immediate_error() -> AsyncIterable[int]:
        raise RuntimeError("Immediate failure")
        yield  # Never reached
    
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_peekable(
            client, worker, streaming_activity_immediate_error, 
            additional_activities=[streaming_activity_immediate_error]
        )
    
    error = assert_streaming_activity_application_error(exc_info.value)
    assert "Immediate failure" in str(error)


async def test_streaming_activity_timeout(client: Client, worker: ExternalWorker):
    """Test streaming activity timeout behavior."""
    @temporalio.activity.defn
    async def streaming_activity_slow() -> AsyncIterable[int]:
        for i in range(3):
            await asyncio.sleep(2)  # 2 seconds per item, will timeout
            yield i
    
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _execute_streaming_workflow_peekable(
            client, worker, streaming_activity_slow,
            schedule_to_close_timeout_ms=3000,  # 3 second timeout, too short
            additional_activities=[streaming_activity_slow]
        )
    
    # Should get a timeout error
    error = assert_streaming_activity_error(exc_info.value)
    assert isinstance(error, (temporalio.exceptions.TimeoutError, temporalio.exceptions.ActivityError))


# Multiple streaming activities tests
async def test_multiple_streaming_activities_sequential_peekable(client: Client, worker: ExternalWorker):
    """Test multiple streaming activities executed sequentially with peekable=True."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [streaming_activity_10_items, streaming_activity_mixed_types],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    @temporalio.workflow.defn
    class MultipleStreamingWorkflow:
        @temporalio.workflow.run
        async def run(self) -> dict:
            # First activity
            int_results = []
            async for item in await temporalio.workflow.execute_local_activity(
                streaming_activity_10_items,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=True
            ):
                int_results.append(item)
            
            # Second activity (different type)
            str_results = []
            async for item in await temporalio.workflow.execute_local_activity(
                streaming_activity_mixed_types,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=True
            ):
                str_results.append(item)
                
            return {"integers": int_results, "strings": str_results}
    
    async with Worker(**worker_config, workflows=[MultipleStreamingWorkflow]):
        handle = await client.start_workflow(
            MultipleStreamingWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        
        assert result["integers"] == list(range(10))
        assert result["strings"] == ["hello", "world", "streaming", "activity", "test"]


async def test_multiple_streaming_activities_sequential_buffered(client: Client, worker: ExternalWorker):
    """Test multiple streaming activities executed sequentially with peekable=False."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [empty_streaming_activity, streaming_activity_10_items],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    @temporalio.workflow.defn
    class MultipleBufferedStreamingWorkflow:
        @temporalio.workflow.run
        async def run(self) -> dict:
            # First activity (empty)
            empty_result = []
            empty_iterable = await temporalio.workflow.execute_local_activity(
                empty_streaming_activity,
                schedule_to_close_timeout=timedelta(seconds=5),
                peekable=False
            )
            async for item in empty_iterable:
                empty_result.append(item)
            
            # Second activity (with items)
            items_result = []
            items_iterable = await temporalio.workflow.execute_local_activity(
                streaming_activity_10_items,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=False
            )
            async for item in items_iterable:
                items_result.append(item)
                
            return {"empty": empty_result, "items": items_result}
    
    async with Worker(**worker_config, workflows=[MultipleBufferedStreamingWorkflow]):
        handle = await client.start_workflow(
            MultipleBufferedStreamingWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        
        assert result["empty"] == []
        assert result["items"] == list(range(10))


# Integration with regular activities
async def test_streaming_and_regular_activities_mixed(client: Client, worker: ExternalWorker):
    """Test workflow with both streaming and regular activities."""
    worker_config = {
        "client": client,
        "task_queue": str(uuid.uuid4()),
        "activities": [streaming_activity_10_items, regular_activity, regular_activity_with_args],
        "shared_state_manager": _default_shared_state_manager,
        "max_concurrent_activities": 50,
    }
    
    @temporalio.workflow.defn
    class MixedActivitiesWorkflow:
        @temporalio.workflow.run
        async def run(self) -> dict:
            # Regular activity
            regular_result = await temporalio.workflow.execute_local_activity(
                regular_activity,
                schedule_to_close_timeout=timedelta(seconds=10)
            )
            
            # Streaming activity (peekable)
            streaming_result = []
            async for item in await temporalio.workflow.execute_local_activity(
                streaming_activity_10_items,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=True
            ):
                streaming_result.append(item)
            
            # Another regular activity with args
            regular_with_args_result = await temporalio.workflow.execute_local_activity(
                regular_activity_with_args,
                "mixed",
                3,
                schedule_to_close_timeout=timedelta(seconds=10)
            )
                
            return {
                "regular": regular_result, 
                "streaming": streaming_result,
                "regular_with_args": regular_with_args_result
            }
    
    async with Worker(**worker_config, workflows=[MixedActivitiesWorkflow]):
        handle = await client.start_workflow(
            MixedActivitiesWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker_config["task_queue"],
        )
        result = await handle.result()
        
        assert result["regular"] == [1, 2, 3, 4, 5]
        assert result["streaming"] == list(range(10))
        assert result["regular_with_args"] == ["mixed_0", "mixed_1", "mixed_2"]


# Validation tests  
def test_startlocalactivityinput_streaming_fields():
    """Test that StartLocalActivityInput has streaming fields."""
    from temporalio.worker._interceptor import StartLocalActivityInput
    
    # Test creating input with streaming fields
    input_obj = StartLocalActivityInput(
        activity="test_activity",
        args=[],
        activity_id=None,
        schedule_to_close_timeout=None,
        schedule_to_start_timeout=None,
        start_to_close_timeout=None,
        retry_policy=None,
        local_retry_threshold=None,
        cancellation_type=temporalio.workflow.ActivityCancellationType.TRY_CANCEL,
        headers={},
        summary=None,
        arg_types=None,
        ret_type=None,
        is_streaming=True,
        peekable=True
    )
    
    assert input_obj.is_streaming == True
    assert input_obj.peekable == True


def test_streaming_activity_definition_properties():
    """Test streaming activity definition properties."""
    # Test that streaming activities are properly detected
    streaming_def = temporalio.activity._Definition.must_from_callable(streaming_activity_10_items)
    assert streaming_def.is_streaming is True
    assert streaming_def.is_async is True
    
    # Test regular activity
    regular_def = temporalio.activity._Definition.must_from_callable(regular_activity)
    assert regular_def.is_streaming is False
    assert regular_def.is_async is True
    
    # Test mixed type streaming activity
    mixed_def = temporalio.activity._Definition.must_from_callable(streaming_activity_mixed_types)
    assert mixed_def.is_streaming is True
    assert mixed_def.is_async is True


if __name__ == "__main__":
    # Run a quick validation test
    import asyncio
    import multiprocessing
    
    async def main():
        await test_streaming_activity_detection()
        print("✓ Activity detection test passed")
        
        test_startlocalactivityinput_streaming_fields()
        print("✓ StartLocalActivityInput fields test passed")
        
        print("\nAll basic validation tests passed!")
        print("\nRun full test suite with: uv run python -m pytest tests/worker/test_streaming_activities.py -v")

    asyncio.run(main())