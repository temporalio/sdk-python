"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

from temporalio.contrib.openai_agents.open_ai_data_converter import open_ai_data_converter
from temporalio.contrib.openai_agents.invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents.model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents.temporal_openai_agents import workflow, set_open_ai_agent_temporal_overrides
from temporalio.contrib.openai_agents.trace_interceptor import OpenAIAgentsTracingInterceptor

__all__ = [
    "open_ai_data_converter",
    "ModelActivity",
    "ModelActivityParameters",
    "workflow",
    "set_open_ai_agent_temporal_overrides",
    "OpenAIAgentsTracingInterceptor",
]



