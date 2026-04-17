"""Temporal Integration for ADK.

This module provides the necessary components to run ADK Agents within Temporal Workflows.
"""

from temporalio.contrib.google_adk_agents._mcp import (
    TemporalMcpToolSet,
    TemporalMcpToolSetProvider,
)
from temporalio.contrib.google_adk_agents._model import TemporalModel
from temporalio.contrib.google_adk_agents._plugin import (
    GoogleAdkPlugin,
)

__all__ = [
    "GoogleAdkPlugin",
    "TemporalMcpToolSet",
    "TemporalMcpToolSetProvider",
    "TemporalModel",
]
