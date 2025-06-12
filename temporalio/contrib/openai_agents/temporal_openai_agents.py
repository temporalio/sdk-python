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
    """Should be called in the main entry point of the application.

    The intended usage is:

    with set_open_ai_agent_temporal_overrides():
      # Initialize the Temporal client and start the worker.

    TODO: Consider wrapping the worker instead of this method.
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
