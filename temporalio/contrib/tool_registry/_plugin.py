"""ToolRegistryPlugin — Temporal plugin for LLM tool-calling activities.

Configures the worker's sandbox to pass through ``anthropic`` and ``openai``
imports, which are otherwise blocked by the Temporal workflow sandbox.  Apply
this plugin when using :func:`run_tool_loop` or :class:`AgenticSession` in an
activity that runs alongside a sandboxed workflow worker.

Example::

    from temporalio.worker import Worker
    from temporalio.contrib.tool_registry import ToolRegistryPlugin

    worker = Worker(
        client,
        task_queue="my-queue",
        plugins=[ToolRegistryPlugin(provider="anthropic")],
        workflows=[MyWorkflow],
        activities=[my_llm_activity],
    )
"""

from __future__ import annotations

import dataclasses

from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


class ToolRegistryPlugin(SimplePlugin):
    """Temporal plugin that configures sandbox passthrough for LLM imports.

    The Temporal workflow sandbox blocks imports of third-party packages such
    as ``anthropic`` and ``openai``.  This plugin adds passthrough rules so
    that activities using those libraries can be registered on the same worker
    as sandboxed workflows without triggering import errors.

    Args:
        provider: LLM provider to configure passthrough for.  Either
            ``"anthropic"``, ``"openai"``, or ``"both"`` (default:
            ``"anthropic"``).
        anthropic_model: Default Anthropic model name passed through to
            :class:`_providers.AnthropicProvider` when not overridden.
        openai_model: Default OpenAI model name passed through to
            :class:`_providers.OpenAIProvider` when not overridden.

    Example::

        plugin = ToolRegistryPlugin(provider="anthropic")
        worker = Worker(client, task_queue="q", plugins=[plugin], ...)
    """

    def __init__(
        self,
        provider: str = "anthropic",
        anthropic_model: str = "claude-sonnet-4-6",
        openai_model: str = "gpt-4o",
    ) -> None:
        """Initialize ToolRegistryPlugin with sandbox passthrough rules."""
        self._provider = provider
        self._anthropic_model = anthropic_model
        self._openai_model = openai_model

        passthrough: list[str] = []
        if provider in ("anthropic", "both"):
            passthrough.append("anthropic")
        if provider in ("openai", "both"):
            passthrough.append("openai")

        def _workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if runner is None:
                raise ValueError("No WorkflowRunner provided to ToolRegistryPlugin.")
            if isinstance(runner, SandboxedWorkflowRunner) and passthrough:
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        *passthrough
                    ),
                )
            return runner

        super().__init__(
            name="ToolRegistryPlugin",
            workflow_runner=_workflow_runner,
        )
