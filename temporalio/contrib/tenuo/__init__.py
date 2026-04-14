"""Tenuo warrant-based authorization for Temporal workflows.

This module provides :class:`TenuoPlugin`, a Temporal :class:`~temporalio.plugin.SimplePlugin`
that wires Tenuo's warrant-based authorization into a Temporal client and worker.

All other Tenuo types (``TenuoPluginConfig``, ``EnvKeyResolver``,
``execute_workflow_authorized``, ``tenuo_execute_activity``, etc.) should be
imported directly from ``tenuo.temporal``::

    from temporalio.contrib.tenuo import TenuoPlugin
    from tenuo.temporal import TenuoPluginConfig, EnvKeyResolver

See the `Tenuo Temporal guide <https://tenuo.ai/docs/temporal>`_ for full
documentation.
"""

from temporalio.contrib.tenuo._plugin import (
    TENUO_PLUGIN_NAME,
    TenuoPlugin,
    ensure_tenuo_workflow_runner,
)

__all__ = [
    "TENUO_PLUGIN_NAME",
    "TenuoPlugin",
    "ensure_tenuo_workflow_runner",
]
