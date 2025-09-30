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
        StatefulMCPServerProvider,
        StatelessMCPServerProvider,
    )
except ImportError:
    pass

from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
    TestModel,
    TestModelProvider,
)
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsTracingInterceptor,
)
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError

from . import workflow

__all__ = [
    "AgentsWorkflowError",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIPayloadConverter",
    "StatelessMCPServerProvider",
    "StatefulMCPServerProvider",
    "TestModel",
    "TestModelProvider",
    "workflow",
]
