"""Unit tests for LangGraph Functional API future implementations.

Tests for TemporalTaskFuture and InlineFuture classes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestTemporalTaskFuture:
    """Tests for TemporalTaskFuture class."""

    def test_initial_state(self) -> None:
        """Future should start in not-done state."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        assert future.done() is False
        assert future.cancelled() is False
        assert future.running() is False

    def test_initial_state_with_handle(self) -> None:
        """Future should be running when initialized with handle."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        mock_handle = MagicMock()
        future: TemporalTaskFuture[int] = TemporalTaskFuture(
            activity_handle=mock_handle
        )

        assert future.done() is False
        assert future.running() is True

    def test_set_result(self) -> None:
        """set_result should mark future as done with result."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        future.set_result(42)

        assert future.done() is True
        assert future.result() == 42
        assert future.exception() is None

    def test_set_exception(self) -> None:
        """set_exception should mark future as done with exception."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()
        error = ValueError("Test error")

        future.set_exception(error)

        assert future.done() is True
        assert future.exception() is error

        with pytest.raises(ValueError, match="Test error"):
            future.result()

    def test_set_activity_handle(self) -> None:
        """set_activity_handle should update the handle."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()
        mock_handle = MagicMock()

        future.set_activity_handle(mock_handle)

        assert future._activity_handle is mock_handle
        assert future.running() is True

    def test_exception_when_not_done(self) -> None:
        """exception() should return None when not done."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        assert future.exception() is None

    def test_result_raises_when_not_done_no_handle(self) -> None:
        """result() should raise when not done and no handle."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        with pytest.raises(RuntimeError, match="not properly initialized"):
            future.result()

    def test_result_raises_when_has_handle(self) -> None:
        """result() should raise when has handle (can't block on async)."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        mock_handle = MagicMock()
        future: TemporalTaskFuture[int] = TemporalTaskFuture(
            activity_handle=mock_handle
        )

        with pytest.raises(RuntimeError, match="Cannot block"):
            future.result()

    def test_add_done_callback_not_implemented(self) -> None:
        """add_done_callback should raise NotImplementedError."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        with pytest.raises(NotImplementedError):
            future.add_done_callback(lambda f: None)

    @pytest.mark.asyncio
    async def test_await_with_result_already_set(self) -> None:
        """await should return result when already set."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()
        future.set_result(42)

        result = await future

        assert result == 42

    @pytest.mark.asyncio
    async def test_await_with_exception_already_set(self) -> None:
        """await should raise exception when already set."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()
        future.set_exception(ValueError("Test error"))

        with pytest.raises(ValueError, match="Test error"):
            await future

    @pytest.mark.asyncio
    async def test_await_without_handle_raises(self) -> None:
        """await should raise when no handle and not done."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        future: TemporalTaskFuture[int] = TemporalTaskFuture()

        with pytest.raises(RuntimeError, match="no activity handle"):
            await future

    @pytest.mark.asyncio
    async def test_await_with_activity_handle(self) -> None:
        """await should delegate to activity handle."""
        from temporalio.contrib.langgraph._functional_future import TemporalTaskFuture

        # Create an async function that returns a value
        async def get_result() -> int:
            return 99

        # Create a proper awaitable mock
        class MockHandle:
            def __await__(self):
                return get_result().__await__()

        mock_handle = MockHandle()

        future: TemporalTaskFuture[int] = TemporalTaskFuture(
            activity_handle=mock_handle  # type: ignore[arg-type]
        )

        result = await future

        assert result == 99
        assert future.done() is True
        assert future._result_value == 99


class TestInlineFuture:
    """Tests for InlineFuture class."""

    def test_with_result(self) -> None:
        """InlineFuture should immediately have result available."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        future: InlineFuture[int] = InlineFuture(result=42)

        assert future.done() is True
        assert future.cancelled() is False
        assert future.running() is False
        assert future.result() == 42
        assert future.exception() is None

    def test_with_exception(self) -> None:
        """InlineFuture should immediately have exception available."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        error = ValueError("Inline error")
        future: InlineFuture[int] = InlineFuture(exception=error)

        assert future.done() is True
        assert future.exception() is error

        with pytest.raises(ValueError, match="Inline error"):
            future.result()

    def test_with_none_result(self) -> None:
        """InlineFuture should handle None result."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        future: InlineFuture[None] = InlineFuture(result=None)

        assert future.result() is None
        assert future.exception() is None

    def test_add_done_callback_not_implemented(self) -> None:
        """add_done_callback should raise NotImplementedError."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        future: InlineFuture[int] = InlineFuture(result=42)

        with pytest.raises(NotImplementedError):
            future.add_done_callback(lambda f: None)

    @pytest.mark.asyncio
    async def test_await_with_result(self) -> None:
        """await should immediately return result."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        future: InlineFuture[int] = InlineFuture(result=42)

        result = await future

        assert result == 42

    @pytest.mark.asyncio
    async def test_await_with_exception(self) -> None:
        """await should immediately raise exception."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        future: InlineFuture[int] = InlineFuture(exception=ValueError("Test"))

        with pytest.raises(ValueError, match="Test"):
            await future

    def test_complex_result_types(self) -> None:
        """InlineFuture should handle complex result types."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture

        complex_result = {
            "data": [1, 2, 3],
            "nested": {"key": "value"},
        }
        future: InlineFuture[dict] = InlineFuture(result=complex_result)

        assert future.result() == complex_result
        assert future.result()["nested"]["key"] == "value"
