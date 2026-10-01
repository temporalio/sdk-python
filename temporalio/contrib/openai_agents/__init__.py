"""Compatibility imports for the standalone OpenAI Agents integration.

Install ``temporalio-openai-agents`` and import ``temporalio.openai_agents``
directly in new code.
"""

from temporalio.openai_agents import (  # pyright: ignore[reportMissingImports]
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
