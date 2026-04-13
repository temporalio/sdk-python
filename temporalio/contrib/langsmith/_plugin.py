"""LangSmith plugin for Temporal SDK."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import langsmith

from temporalio.contrib.langsmith._interceptor import LangSmithInterceptor
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


class LangSmithPlugin(SimplePlugin):
    """LangSmith tracing plugin for Temporal SDK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Provides automatic LangSmith run creation for workflows, activities,
    and other Temporal operations with context propagation.
    """

    def __init__(
        self,
        *,
        client: langsmith.Client | None = None,
        project_name: str | None = None,
        add_temporal_runs: bool = False,
        default_metadata: dict[str, Any] | None = None,
        default_tags: list[str] | None = None,
    ) -> None:
        """Initialize the LangSmith plugin.

        Args:
            client: A langsmith.Client instance. If None, one will be created
                automatically (using LANGSMITH_API_KEY env var).
            project_name: LangSmith project name for traces.
            add_temporal_runs: Whether to create LangSmith runs for Temporal
                operations. Defaults to False.
            default_metadata: Default metadata to attach to all runs.
            default_tags: Default tags to attach to all runs.
        """
        interceptor = LangSmithInterceptor(
            client=client,
            project_name=project_name,
            add_temporal_runs=add_temporal_runs,
            default_metadata=default_metadata,
            default_tags=default_tags,
        )
        interceptors = [interceptor]

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the LangSmith plugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "langsmith"
                    ),
                )
            return runner

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            try:
                yield
            finally:
                interceptor._client.flush()

        super().__init__(
            "langchain.LangSmithPlugin",
            interceptors=interceptors,
            workflow_runner=workflow_runner,
            run_context=run_context,
        )
