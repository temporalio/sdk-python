"""Initialize Temporal OpenAI Agents overrides."""

from contextlib import contextmanager
from datetime import timedelta
from typing import Optional

from agents import set_trace_provider
from agents.run import get_default_agent_runner, set_default_agent_runner
from agents.tracing import get_trace_provider
from agents.tracing.provider import DefaultTraceProvider

from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@contextmanager
def set_open_ai_agent_temporal_overrides(
    *,
    task_queue: Optional[str] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    retry_policy: Optional[RetryPolicy] = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: Optional[str] = None,
    versioning_intent: Optional[VersioningIntent] = None,
    summary: Optional[str] = None,
    priority: Priority = Priority.default,
):
    """Configure Temporal-specific overrides for OpenAI agents.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments. Future versions may wrap the worker directly
        instead of requiring this context manager.

    This context manager sets up the necessary Temporal-specific runners and trace providers
    for running OpenAI agents within Temporal workflows. It should be called in the main
    entry point of your application before initializing the Temporal client and worker.

    The context manager handles:
    1. Setting up a Temporal-specific runner for OpenAI agents
    2. Configuring a Temporal-aware trace provider
    3. Restoring previous settings when the context exits

    Args:
        task_queue: Specific task queue to use for model activities.
        schedule_to_close_timeout: Maximum time from scheduling to completion.
        schedule_to_start_timeout: Maximum time from scheduling to starting.
        start_to_close_timeout: Maximum time for the activity to complete.
        heartbeat_timeout: Maximum time between heartbeats.
        retry_policy: Policy for retrying failed activities.
        cancellation_type: How the activity handles cancellation.
        activity_id: Unique identifier for the activity instance.
        versioning_intent: Versioning intent for the activity.
        summary: Summary for the activity execution.
        priority: Priority for the activity execution.

    Example usage:
        with set_open_ai_agent_temporal_overrides(
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3)
        ):
            # Initialize Temporal client and worker here
            client = await Client.connect("localhost:7233")
            worker = Worker(client, task_queue="my-task-queue")
            await worker.run()

    Returns:
        A context manager that yields the configured TemporalTraceProvider.

    """
    previous_runner = get_default_agent_runner()
    previous_trace_provider = get_trace_provider()
    provider = TemporalTraceProvider()

    try:
        set_default_agent_runner(
            TemporalOpenAIRunner(
                task_queue=task_queue,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                cancellation_type=cancellation_type,
                activity_id=activity_id,
                versioning_intent=versioning_intent,
                summary=summary,
                priority=priority,
            )
        )
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_agent_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())
