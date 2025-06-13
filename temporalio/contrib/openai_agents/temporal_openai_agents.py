"""Initialize Temporal OpenAI Agents overrides."""

from contextlib import contextmanager
from typing import Optional

from agents import set_default_runner, set_trace_provider
from agents.run import Runner, get_default_runner
from agents.tracing import TraceProvider, get_trace_provider
from agents.tracing.provider import DefaultTraceProvider

from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)


@contextmanager
def set_open_ai_agent_temporal_overrides():
    """Configure Temporal-specific overrides for OpenAI agents.

    This context manager sets up the necessary Temporal-specific runners and trace providers
    for running OpenAI agents within Temporal workflows. It should be called in the main
    entry point of your application before initializing the Temporal client and worker.

    The context manager handles:
    1. Setting up a Temporal-specific runner for OpenAI agents
    2. Configuring a Temporal-aware trace provider
    3. Restoring previous settings when the context exits

    Example usage:
        with set_open_ai_agent_temporal_overrides():
            # Initialize Temporal client and worker here
            client = await Client.connect("localhost:7233")
            worker = Worker(client, task_queue="my-task-queue")
            await worker.run()

    Returns:
        A context manager that yields the configured TemporalTraceProvider.

    Note:
        This is a temporary solution. Future versions may wrap the worker directly
        instead of requiring this context manager.
    """
    previous_runner: Optional[Runner] = None
    previous_trace_provider: Optional[TraceProvider] = None
    try:
        previous_runner = get_default_runner()
        set_default_runner(TemporalOpenAIRunner())

        provider = TemporalTraceProvider()
        previous_trace_provider = get_trace_provider()
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())
