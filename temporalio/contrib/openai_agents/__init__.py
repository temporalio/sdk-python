"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

from temporalio.contrib.openai_agents._invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
    TestModel,
    TestModelProvider,
    set_open_ai_agent_temporal_overrides,
)
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsTracingInterceptor,
)

from . import workflow

__all__ = [
    "ModelActivity",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIAgentsTracingInterceptor",
    "set_open_ai_agent_temporal_overrides",
    "TestModel",
    "TestModelProvider",
    "workflow",
]
