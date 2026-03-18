"""Tests for the CachingInterceptor with real Temporal workers."""

import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.contrib.activity_cache import CachingInterceptor, no_cache
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Track call counts across activities
_call_counts: dict[str, int] = {}


@activity.defn
async def add(x: int, y: int) -> int:
    """Activity that tracks how many times it's called."""
    _call_counts["add"] = _call_counts.get("add", 0) + 1
    return x + y


@no_cache
@activity.defn
async def add_no_cache(x: int, y: int) -> int:
    """Activity opted out of caching."""
    _call_counts["add_no_cache"] = _call_counts.get("add_no_cache", 0) + 1
    return x + y


@workflow.defn
class CallTwiceWorkflow:
    """Calls the same activity twice with the same args."""

    @workflow.run
    async def run(self, x: int, y: int) -> list[int]:
        r1 = await workflow.execute_activity(
            add,
            args=[x, y],
            start_to_close_timeout=timedelta(seconds=30),
        )
        r2 = await workflow.execute_activity(
            add,
            args=[x, y],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return [r1, r2]


@workflow.defn
class CallNoCacheWorkflow:
    """Calls a @no_cache activity twice."""

    @workflow.run
    async def run(self, x: int, y: int) -> list[int]:
        r1 = await workflow.execute_activity(
            add_no_cache,
            args=[x, y],
            start_to_close_timeout=timedelta(seconds=30),
        )
        r2 = await workflow.execute_activity(
            add_no_cache,
            args=[x, y],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return [r1, r2]


class TestCachingInterceptor:
    """Tests for CachingInterceptor with real Temporal workers."""

    @pytest.fixture(autouse=True)
    def reset_counts(self) -> None:
        """Reset call counts before each test."""
        _call_counts.clear()

    @pytest.fixture
    def store_url(self) -> str:
        """Unique memory:// URL per test."""
        return f"memory://interceptor-test/{uuid.uuid4()}"

    @pytest.fixture
    async def env(self) -> WorkflowEnvironment:
        """Start a local Temporal test environment."""
        return await WorkflowEnvironment.start_local()

    async def test_interceptor_caches_activity(
        self, env: WorkflowEnvironment, store_url: str
    ) -> None:
        """Second call with same args is served from cache."""
        task_queue = str(uuid.uuid4())

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CallTwiceWorkflow],
            activities=[add],
            interceptors=[CachingInterceptor(store_url)],
        ):
            result = await env.client.execute_workflow(
                CallTwiceWorkflow.run,
                args=[3, 4],
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )

        assert result == [7, 7]
        assert _call_counts["add"] == 1  # Only called once, second was cached

    async def test_no_cache_skips_caching(
        self, env: WorkflowEnvironment, store_url: str
    ) -> None:
        """@no_cache activities always execute."""
        task_queue = str(uuid.uuid4())

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CallNoCacheWorkflow],
            activities=[add_no_cache],
            interceptors=[CachingInterceptor(store_url)],
        ):
            result = await env.client.execute_workflow(
                CallNoCacheWorkflow.run,
                args=[3, 4],
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )

        assert result == [7, 7]
        assert _call_counts["add_no_cache"] == 2  # Called both times
