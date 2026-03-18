"""Tests for the @cached decorator."""

import uuid
from datetime import timedelta

import pytest

from temporalio.contrib.activity_cache import cached


class TestCachedDecorator:
    """Tests for the @cached decorator with memory:// backend."""

    @pytest.fixture
    def store_url(self) -> str:
        """Unique memory:// URL per test."""
        return f"memory://cache-test/{uuid.uuid4()}"

    async def test_cache_hit(self, store_url: str) -> None:
        """Second call with same args returns cached result."""
        calls = 0

        @cached(store_url)
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        assert await fn(5) == 10
        assert calls == 1
        assert await fn(5) == 10
        assert calls == 1  # Not called again

    async def test_cache_miss_on_different_args(self, store_url: str) -> None:
        """Different args cause a cache miss."""
        calls = 0

        @cached(store_url)
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        assert await fn(5) == 10
        assert await fn(6) == 12
        assert calls == 2

    async def test_ttl_expiry(self, store_url: str) -> None:
        """Expired entries are treated as misses."""
        calls = 0

        @cached(store_url, ttl=timedelta(seconds=-1))  # Already expired
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        assert await fn(1) == 1
        assert calls == 1
        assert await fn(1) == 1
        assert calls == 2  # Called again because first entry expired

    async def test_key_fn(self, store_url: str) -> None:
        """key_fn controls which args are in the cache key."""
        calls = 0

        @cached(store_url, key_fn=lambda x, _ignored: {"x": x})
        async def fn(x: int, ignored: str) -> int:
            nonlocal calls
            calls += 1
            return x

        assert await fn(1, "a") == 1
        assert calls == 1
        assert await fn(1, "b") == 1  # Different ignored arg, same key
        assert calls == 1

    async def test_preserves_function_name(self, store_url: str) -> None:
        """Wrapper preserves the original function name."""

        @cached(store_url)
        async def my_function(x: int) -> int:
            return x

        assert my_function.__name__ == "my_function"

    async def test_complex_return_values(self, store_url: str) -> None:
        """Complex return types survive pickle round-trip."""

        @cached(store_url)
        async def fn() -> dict:
            return {"nested": [1, 2, {"deep": True}], "set_like": [1, 2, 3]}

        result1 = await fn()
        result2 = await fn()
        assert result1 == result2
        assert result1 == {"nested": [1, 2, {"deep": True}], "set_like": [1, 2, 3]}
