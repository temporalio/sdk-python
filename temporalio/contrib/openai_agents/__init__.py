"""Compatibility imports for the standalone OpenAI Agents integration.

New code should import :mod:`temporalio.openai_agents` directly.

.. deprecated::
    Install ``temporalio-openai-agents`` and import
    :mod:`temporalio.openai_agents` instead.
"""

import warnings

from temporalio.openai_agents import (
    AgentsWorkflowError,
    AllowAllWorkerEnvVars,
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
    SandboxClientProvider,
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
    TemporalWorkerEnvValue,
    temporal_worker_env_ref,
)

from . import testing, workflow

warnings.warn(
    "temporalio.contrib.openai_agents is deprecated; install "
    "temporalio-openai-agents and import temporalio.openai_agents instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "AgentsWorkflowError",
    "AllowAllWorkerEnvVars",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIPayloadConverter",
    "SandboxClientProvider",
    "StatelessMCPServerProvider",
    "StatefulMCPServerProvider",
    "TemporalWorkerEnvValue",
    "temporal_worker_env_ref",
    "testing",
    "workflow",
]
