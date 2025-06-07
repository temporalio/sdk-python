from agents import set_default_runner, set_trace_provider

from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)


def set_open_ai_agent_temporal_overrides() -> TemporalTraceProvider:
    """
    TODO: Revert runner on __exit__ to the previous one.
    TODO: Consider wrapping the worker instead of this method.
    """
    set_default_runner(TemporalOpenAIRunner())
    provider = TemporalTraceProvider()
    set_trace_provider(provider)
    return provider
