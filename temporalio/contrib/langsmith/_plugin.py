"""LangSmith plugin for Temporal SDK."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import langsmith

import temporalio.client
from temporalio.contrib.langsmith._interceptor import LangSmithInterceptor
from temporalio.plugin import SimplePlugin, WorkerConfig
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


class _ClientOnlyLangSmithInterceptor(temporalio.client.Interceptor):
    """Wrapper that exposes only the client interceptor interface.

    This prevents ``_init_from_config`` from detecting it as a
    ``worker.Interceptor`` and automatically pulling it into every worker.
    Each worker gets its own ``LangSmithInterceptor`` via
    ``LangSmithPlugin.configure_worker`` instead.
    """

    def __init__(self, interceptor: LangSmithInterceptor) -> None:
        super().__init__()
        self._interceptor = interceptor

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """Delegate to the wrapped interceptor."""
        return self._interceptor.intercept_client(next)


class LangSmithPlugin(SimplePlugin):
    """LangSmith tracing plugin for Temporal SDK.

    Provides automatic LangSmith run creation for workflows, activities,
    and other Temporal operations with context propagation.
    """

    def __init__(
        self,
        *,
        client: langsmith.Client | None = None,
        project_name: str | None = None,
        add_temporal_runs: bool = False,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Initialize the LangSmith plugin.

        Args:
            client: A langsmith.Client instance. If None, one will be created
                lazily (using LANGSMITH_API_KEY env var).
            project_name: LangSmith project name for traces.
            add_temporal_runs: Whether to create LangSmith runs for Temporal
                operations. Defaults to False.
            metadata: Default metadata to attach to all runs.
            tags: Default tags to attach to all runs.
        """

        def make_interceptor() -> LangSmithInterceptor:
            return LangSmithInterceptor(
                client=client,
                project_name=project_name,
                add_temporal_runs=add_temporal_runs,
                default_metadata=metadata,
                default_tags=tags,
            )

        wrapper = _ClientOnlyLangSmithInterceptor(make_interceptor())
        ls_client = wrapper._interceptor._client
        self._make_interceptor = make_interceptor

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
                ls_client.flush()

        super().__init__(
            "langchain.LangSmithPlugin",
            interceptors=[wrapper],
            workflow_runner=workflow_runner,
            run_context=run_context,
        )

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """Create a fresh LangSmithInterceptor for each worker."""
        config = super().configure_worker(config)
        worker_interceptor = self._make_interceptor()
        interceptors = list(config.get("interceptors") or [])
        interceptors.append(worker_interceptor)
        config["interceptors"] = interceptors
        return config
