"""LangGraph plugin for Temporal SDK.

.. warning::
    This package is experimental and may change in future versions.
    Use with caution in production environments.

This plugin runs `LangGraph <https://github.com/langchain-ai/langgraph>`_ nodes
and tasks as Temporal Activities, giving your AI agent workflows durable
execution, automatic retries, and timeouts. It supports both the LangGraph Graph
API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).
"""

from temporalio.contrib.langgraph._plugin import (
    LangGraphPlugin,
    cache,
    entrypoint,
    graph,
)

__all__ = [
    "LangGraphPlugin",
    "entrypoint",
    "cache",
    "graph",
]
