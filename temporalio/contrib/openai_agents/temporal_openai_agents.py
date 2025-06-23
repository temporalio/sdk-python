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
from temporalio.contrib.openai_agents.model_parameters import ModelActivityParameters
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@contextmanager
def set_open_ai_agent_temporal_overrides(
    model_params: ModelActivityParameters,
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
        model_params: Configuration parameters for Temporal activity execution of model calls.

    Example usage:
        model_params = ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3)
        )
        with set_open_ai_agent_temporal_overrides(model_params):
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
            TemporalOpenAIRunner(model_params)
        )
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_agent_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())
