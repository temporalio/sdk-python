"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

# Best Effort mcp, as it is not supported on Python 3.9
try:
    from temporalio.contrib.openai_agents._mcp import (
        TemporalMCPServer,
        TemporalMCPServerWorkflowShim,
    )
except ImportError:
    pass

from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
    TestModel,
    TestModelProvider,
)
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsTracingInterceptor,
)

from . import workflow

__all__ = [
    "OpenAIAgentsPlugin",
    "ModelActivityParameters",
    "workflow",
    "TemporalMCPServer",
    "TemporalMCPServerWorkflowShim",
    "TestModel",
    "TestModelProvider",
]
