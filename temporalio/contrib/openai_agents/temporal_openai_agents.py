"""Initialize Temporal OpenAI Agents overrides."""

from contextlib import contextmanager
from typing import Optional

from agents import set_trace_provider
from agents.run import AgentRunner, get_default_agent_runner, set_default_agent_runner
from agents.tracing import TraceProvider, get_trace_provider
from agents.tracing.provider import DefaultTraceProvider

from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)


@contextmanager
def set_open_ai_agent_temporal_overrides(**kwargs):
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
        **kwargs: Additional arguments to pass to the TemporalOpenAIRunner constructor.
            These arguments are forwarded to workflow.execute_activity_method when
            executing model calls. Common options include:
            - start_to_close_timeout: Maximum time for the activity to complete
            - schedule_to_close_timeout: Maximum time from scheduling to completion
            - retry_policy: Policy for retrying failed activities
            - task_queue: Specific task queue to use for model activities

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
        set_default_agent_runner(TemporalOpenAIRunner(**kwargs))
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_agent_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())
