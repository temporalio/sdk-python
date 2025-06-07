"""Initialize Temporal OpenAI Agents overrides."""

from agents import set_default_runner, set_trace_provider

from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)


def set_open_ai_agent_temporal_overrides() -> TemporalTraceProvider:
    """Should be called in the main entry point of the application.

    The intended usage is:

    with set_open_ai_agent_temporal_overrides():
      # Initialize the Temporal client and start the worker.

    TODO: Revert runner on __exit__ to the previous one.
    TODO: Consider wrapping the worker instead of this method.
    """
    set_default_runner(TemporalOpenAIRunner())
    provider = TemporalTraceProvider()
    set_trace_provider(provider)
    return provider
