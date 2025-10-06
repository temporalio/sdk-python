"""
Streaming infrastructure for streaming activities.

This module provides the queue-based communication system for delivering
streaming results from activities to workflows in real-time.
"""

import asyncio
from typing import Any, Dict, Optional, Tuple

# Global registry for local activity streaming queues
# Key: (workflow_run_id, activity_id) -> StreamingResultQueue
_local_streaming_queues: Dict[Tuple[str, str], "StreamingResultQueue"] = {}


def register_local_streaming_queue(
    workflow_run_id: str, activity_id: str, queue: "StreamingResultQueue"
) -> None:
    """Register a streaming queue for a local activity."""
    key = (workflow_run_id, activity_id)
    _local_streaming_queues[key] = queue


def lookup_local_streaming_queue(
    workflow_run_id: str, activity_id: str
) -> Optional["StreamingResultQueue"]:
    """Look up a streaming queue for a local activity."""
    key = (workflow_run_id, activity_id)
    result = _local_streaming_queues.get(key)
    return result


def unregister_local_streaming_queue(
    workflow_run_id: str, activity_id: str
) -> None:
    """Clean up a streaming queue registration."""
    key = (workflow_run_id, activity_id)
    _local_streaming_queues.pop(key, None)


class StreamingResultQueue:
    """Queue for delivering streaming results from activities to workflows.
    
    This queue enables real-time delivery of streaming activity results to 
    workflows when peekable=True is used. The queue supports both streaming
    items and completion signaling.
    """

    def __init__(self, activity_id: str):
        """Initialize a streaming result queue for an activity.
        
        Args:
            activity_id: Unique identifier for the activity using this queue.
        """
        self._activity_id = activity_id
        self._queue: asyncio.Queue[Tuple[str, ...]] = asyncio.Queue()
        self._completed = False
        self._error: Optional[Exception] = None
        
    async def put_item(self, item: Any) -> None:
        """Add a streaming result item to the queue.
        
        Args:
            item: The result item to add to the queue.
        """
        if not self._completed:
            await self._queue.put(('item', item))
    
    async def put_completion(self, success: bool, error: Optional[Exception] = None) -> None:
        """Mark the stream as completed.
        
        Args:
            success: True if the activity completed successfully, False if it failed.
            error: Exception that caused the failure (if success=False).
        """
        self._completed = True
        self._error = error
        await self._queue.put(('complete', success, error))
    
    async def get_next_item(self) -> Tuple[str, ...]:
        """Get the next item from the queue.
        
        Returns:
            Tuple where first element is message type:
            - ('item', value): A streaming result item
            - ('complete', success, error): Stream completion signal
        """
        return await self._queue.get()

    @property
    def activity_id(self) -> str:
        """Get the activity ID this queue belongs to."""
        return self._activity_id
    
    @property
    def completed(self) -> bool:
        """Check if the stream has completed."""
        return self._completed
    
    @property  
    def error(self) -> Optional[Exception]:
        """Get the error that caused failure (if any)."""
        return self._error
    
    def close(self) -> None:
        """Close the queue and clean up resources.
        
        This should be called when the queue is no longer needed
        to ensure proper cleanup and prevent memory leaks.
        """
        # Signal completion if not already done
        if not self._completed:
            try:
                # Use put_nowait since we're in synchronous context
                self._queue.put_nowait(('complete', False, RuntimeError("Queue closed before completion")))
                self._completed = True
            except asyncio.QueueFull:
                pass  # Queue was already full, completion signal might already be there
        
        # Clear the queue to release references
        # Note: There's no official way to clear an asyncio.Queue, 
        # but this will help with garbage collection
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass


class _BufferedAsyncIterator:
    """Async iterator over a pre-computed list of items.
    
    Used for non-peekable streaming activities where all results
    are collected first, then iterated over.
    """
    
    def __init__(self, items: list):
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


def create_buffered_iterator(items: list):
    """Create a buffered async iterator over a list of items."""
    return _BufferedAsyncIterator(items)