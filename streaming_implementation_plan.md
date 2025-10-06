# Streaming Activities Implementation Plan

## Overview

This document outlines the complete implementation plan for streaming activities in the Temporal Python SDK without modifying Rust bridge code. The approach uses dual-path result delivery: normal path to Temporal server for durability, and shortcut path to workflow for peekable activities.

## Current Status

✅ **Phase I Complete**: Basic infrastructure implemented
- Streaming activity detection (`AsyncIterable` return types)
- `peekable` parameter added to `execute_local_activity()`
- Peek mode enforcement (one peekable activity at a time)
- Activity completion cleanup

## Implementation Strategy

### Core Approach: Dual-Path Result Delivery + Real-Time Streaming

The key insight is that streaming activities have **two result destinations**:

1. **Path 1: Normal Activity Completion** → Temporal server (for durability)
2. **Path 2: Real-Time Streaming** → Workflow queue (for peekable activities only)

For local activities, we can implement Path 2 using an in-memory queue/communication mechanism since the activity and workflow run in the same process.

## Detailed Implementation Plan

### 1. Streaming Result Queue Infrastructure

#### 1.1 Streaming Result Queue

**Location**: New module `temporalio/worker/_streaming.py`

**Implementation**: Queue system for real-time result delivery

```python
class StreamingResultQueue:
    """Queue for delivering streaming results from activities to workflows."""
    
    def __init__(self, activity_id: str, workflow_instance: '_WorkflowInstanceImpl'):
        self._activity_id = activity_id
        self._workflow_instance = workflow_instance
        self._queue = asyncio.Queue()
        self._completed = False
        self._error: Optional[Exception] = None
    
    async def put_item(self, item: Any) -> None:
        """Add a streaming result item to the queue."""
        if not self._completed:
            await self._queue.put(('item', item))
    
    async def put_completion(self, success: bool, error: Optional[Exception] = None) -> None:
        """Mark the stream as completed."""
        self._completed = True
        self._error = error
        await self._queue.put(('complete', success, error))
    
    async def get_next_item(self) -> Tuple[str, Any]:
        """Get the next item from the queue. Returns ('item', value) or ('complete', success, error)."""
        return await self._queue.get()
```

#### 1.2 Activity Execution Side

**Location**: `temporalio/worker/_activity.py`

**Implementation**: Enhanced streaming execution with dual-path delivery

```python
async def _execute_streaming_activity(
    self, 
    start, 
    running_activity, 
    task_token, 
    data_converter,
    result_queue: Optional[StreamingResultQueue] = None
) -> List[Any]:
    """Execute streaming activity with dual-path result delivery."""
    
    # Get the activity function result (AsyncIterable)
    activity_result = await self._execute_activity(start, running_activity, task_token, data_converter)
    
    # Collect all items for final result AND stream them to queue
    collected_items = []
    
    try:
        async for item in activity_result:
            # Path 1: Collect for final durable result
            collected_items.append(item)
            
            # Path 2: Stream to workflow queue if peekable
            if result_queue:
                await result_queue.put_item(item)
        
        # Signal completion to queue
        if result_queue:
            await result_queue.put_completion(True)
            
    except Exception as e:
        # On error, discard partial results and signal error to queue
        if result_queue:
            await result_queue.put_completion(False, e)
        raise
    
    return collected_items
```

#### 1.2 Activity Result Encoding

**Location**: `temporalio/worker/_activity.py` around lines 319-323

**Current**:
```python
result = await self._execute_activity(start, running_activity, task_token, data_converter)
[payload] = await data_converter.encode([result])
completion.result.completed.result.CopyFrom(payload)
```

**Enhanced**:
```python
result = await self._execute_streaming_activity_if_needed(start, running_activity, task_token, data_converter)
if isinstance(result, StreamingActivityResult):
    # Encode streaming result with metadata
    [payload] = await data_converter.encode([result.items])
    completion.result.completed.result.CopyFrom(payload)
    # Add streaming metadata to headers
    completion.result.completed.headers["temporal_streaming"] = b"true"
else:
    # Normal activity result handling
    [payload] = await data_converter.encode([result])
    completion.result.completed.result.CopyFrom(payload)
```

### 2. Workflow Side (Real-Time Iterator Implementation)

#### 2.1 Peekable Activity Handle

**Location**: New class in `temporalio/worker/_workflow_instance.py`

**Implementation**: Custom activity handle that supports real-time async iteration

```python
class PeekableActivityHandle(temporalio.workflow.ActivityHandle[AsyncIterable[T]]):
    """Activity handle that supports real-time async iteration over streaming results."""
    
    def __init__(self, instance, input, fn, result_queue: StreamingResultQueue):
        super().__init__(fn)
        self._result_queue = result_queue
        self._completed = False
        self._final_result = None
        
    def __aiter__(self) -> AsyncIterator[T]:
        return self
        
    async def __anext__(self) -> T:
        """Yield items as they arrive from the streaming activity."""
        if self._completed:
            # If we've already completed, no more items
            raise StopAsyncIteration
        
        # Get next item from the streaming queue
        msg_type, *data = await self._result_queue.get_next_item()
        
        if msg_type == 'item':
            # Got a streaming item - yield it
            return data[0]
        elif msg_type == 'complete':
            # Activity completed
            self._completed = True
            success, error = data[0], data[1]
            
            if not success and error:
                # Activity failed - propagate error
                raise error
            
            # No more items - end iteration
            raise StopAsyncIteration
        else:
            raise RuntimeError(f"Unknown message type: {msg_type}")
    
    async def __await__(self):
        """Wait for the activity to complete and return final durable result."""
        if self._final_result is None:
            self._final_result = await super().__await__()
        return self._final_result
```

#### 2.2 Enhanced Activity Resolution

**Location**: `_WorkflowInstanceImpl.workflow_start_local_activity()`

**Implementation**: Return peekable handles for streaming activities

```python
def workflow_start_local_activity(self, activity, *args, peekable=False, **kwargs):
    # Existing validation logic...
    
    if peekable and is_streaming:
        # Return a peekable handle that supports async iteration
        return PeekableActivityHandle(self, input, fn)
    else:
        # Return regular activity handle
        return self._outbound.start_local_activity(input)
```

#### 2.3 Activity Result Processing

**Location**: `_apply_resolve_activity()` method

**Implementation**: Detect streaming results and handle appropriately

```python
def _apply_resolve_activity(self, job):
    # Check if this is a streaming activity result
    is_streaming_result = job.result.completed.headers.get("temporal_streaming") == b"true"
    
    if is_streaming_result:
        # Process streaming result
        ret_vals = self._convert_payloads([job.result.completed.result], ret_types, payload_converter)
        streaming_items = ret_vals[0]  # This is the list of all items
        handle._resolve_streaming_success(streaming_items)
    else:
        # Normal result processing
        # ... existing code
```

### 3. Enhanced Workflow API

#### 3.1 Unified Return Type for Streaming Activities

**Location**: `temporalio/workflow.py`

**Implementation**: All streaming activities return `AsyncIterable[T]` regardless of peekable mode

```python
# Updated overloads - streaming activities always return AsyncIterable[T]
@overload
async def execute_local_activity(
    activity: Callable[..., AsyncIterable[T]],
    *args,
    peekable: bool = False,
    **kwargs
) -> AsyncIterable[T]: ...

# Implementation handles both peekable and non-peekable modes
async def execute_local_activity(activity, *args, peekable=False, **kwargs):
    # Check if this is a streaming activity
    if callable(activity):
        defn = temporalio.activity._Definition.from_callable(activity)
        if defn and defn.is_streaming:
            # For streaming activities, always return AsyncIterable
            handle = _Runtime.current().workflow_start_local_activity(
                activity, *args, peekable=peekable, **kwargs
            )
            
            if peekable:
                # Return peekable handle that streams in real-time
                return handle
            else:
                # Return buffered iterator over final result
                final_result = await handle
                return _create_buffered_iterator(final_result)
    
    # Regular activity - existing behavior
    handle = _Runtime.current().workflow_start_local_activity(
        activity, *args, peekable=peekable, **kwargs
    )
    return await handle

class _BufferedAsyncIterator:
    """Iterator over a pre-computed list of items."""
    def __init__(self, items: List[T]):
        self._items = items
        self._index = 0
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item

def _create_buffered_iterator(items: List[T]) -> AsyncIterable[T]:
    return _BufferedAsyncIterator(items)
```

### 4. Test Implementation

#### 4.1 Basic Functionality Tests

**File**: `tests/worker/test_streaming_activities.py`

```python
@temporalio.activity.defn
async def streaming_activity_10_items() -> AsyncIterable[int]:
    for i in range(10):
        await asyncio.sleep(0.01)
        yield i

@temporalio.workflow.defn
class StreamingTestWorkflow:
    @temporalio.workflow.run
    async def run(self, test_case: str):
        if test_case == "peekable":
            results = []
            async for item in await temporalio.workflow.execute_local_activity(
                streaming_activity_10_items,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=True
            ):
                results.append(item)
            return results
        else:
            # Non-peekable returns all results at once
            return await temporalio.workflow.execute_local_activity(
                streaming_activity_10_items,
                schedule_to_close_timeout=timedelta(seconds=10),
                peekable=False
            )
```

#### 4.2 Comprehensive Test Cases

1. **Basic streaming with peekable=True** - iterate over results
2. **Basic streaming with peekable=False** - get complete list
3. **Empty streaming activity** - no results yielded
4. **Large streaming activity** - 100+ items for performance
5. **Error handling** - exception during streaming
6. **Peek mode enforcement** - second peekable activity fails
7. **Activity cancellation** - cancel during streaming
8. **Regular activity compatibility** - ensure no regression

### 5. Data Structures

#### 5.1 StreamingActivityResult

```python
@dataclass
class StreamingActivityResult:
    items: List[Any]
    streaming: bool = True
    activity_type: Optional[str] = None
```

#### 5.2 Enhanced StartLocalActivityInput

Already implemented with `is_streaming` and `peekable` fields.

### 6. Key Implementation Details

#### 6.1 Determinism and Replay

- **Streaming activities are deterministic** - they collect all results during execution
- **Final result is durable** - stored in workflow history as a complete list  
- **Partial results are non-durable** - only exist during peek mode real-time iteration
- **Replay behavior** - on replay, peek mode is bypassed and the complete list is available immediately
- **Peek mode cannot be exited until activity completes** - enforced by queue completion signaling

#### 6.2 Memory Management

- **No new buffer size limits** - rely on existing activity timeout and payload size limits
- **Stream buffer size unlimited** - but add comment for future consideration
- **Queue cleanup** - ensure streaming queues are properly cleaned up on completion/error
- **Resource cleanup** - ensure iterators and queue resources are properly cleaned up

#### 6.3 Error Handling

- **Partial results discarded on error** - if activity raises exception after yielding some items
- **Activity errors propagate** - through both streaming queue and final activity completion
- **Iterator errors handled** - errors during async iteration properly propagated
- **Cancellation handling** - properly cancel both activity, queue, and iterator
- **Future heartbeat checkpointing** - comment placeholder for checkpoint mechanism

#### 6.4 Type Safety

- **Unified return type** - `AsyncIterable[T]` for all streaming activities regardless of peekable mode
- **Generic type preservation** - streaming activities maintain correct type information
- **Runtime type checking** - validate streaming activity contracts during execution
- **MyPy compatibility** - ensure type hints work correctly for both peekable and non-peekable modes

#### 6.5 Dual-Path Result Delivery

- **Path 1 (Durable)** - normal activity completion to Temporal server
- **Path 2 (Real-time)** - streaming queue for peekable workflows only
- **Local activities only** - queue mechanism works because activity and workflow are in same process
- **Queue-based communication** - asyncio.Queue for thread-safe result delivery
- **Completion signaling** - explicit completion messages distinguish from streaming items

## Implementation Order

1. **Phase 1**: Activity execution side (worker) streaming collection
2. **Phase 2**: Peekable activity handle with async iterator support
3. **Phase 3**: Enhanced workflow API with proper overloads
4. **Phase 4**: Comprehensive test suite
5. **Phase 5**: Error handling and edge cases
6. **Phase 6**: Performance optimization and memory management

## Questions and Considerations

1. **Memory Usage**: For very large streams, should we implement streaming with size limits or memory-mapped storage?

2. **Error Propagation**: How should we handle errors that occur in the middle of streaming vs. at the end?

3. **Cancellation Timing**: Should cancelling a peekable activity stop iteration immediately or allow buffered items to finish?

4. **Type Annotation**: Should we create a special `StreamingActivity` type annotation for better developer experience?

5. **Bridge Compatibility**: This approach maintains bridge compatibility, but should we add metadata to activity task completion to better support future streaming features?

6. **Activity Context**: Should streaming activities have access to special context methods like `activity.yield_partial(item)` for explicit yielding?

## Next Steps

1. Review this plan for completeness and feasibility
2. Confirm approach for complex scenarios (error handling, large streams)
3. Begin implementation starting with Phase 1
4. Create comprehensive test cases covering all scenarios
5. Performance testing with large streaming workloads

This approach provides full streaming functionality while maintaining compatibility with the existing Temporal infrastructure and avoiding Rust bridge modifications.