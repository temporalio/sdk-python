from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import strands.models.model as _strands_model

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from ._temporal_mcp_client import TemporalMCPClient
from ._temporal_model import TemporalModel

# Force Strands' base Model.count_tokens to skip tiktoken (non-deterministic)
# and use its chars-per-token heuristic (deterministic).
setattr(_strands_model, "_get_encoding", lambda: None)


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    Configures sandbox passthrough for ``strands``, ``strands_tools``, ``mcp``,
    and ``temporalio.contrib.strands`` (so the MCP tool cache is visible to
    workflow code), and swaps in ``pydantic_data_converter`` so structured
    outputs serialize.

    When ``model`` is supplied, calls its ``model_factory`` once on the worker
    to construct the real model, then registers the model invocation activities
    against it. The same :class:`TemporalModel` is also passed to
    ``Agent(model=...)`` inside the workflow.

    When ``mcp_clients`` is supplied, registers per-server ``{server}-call-tool``
    activities and, at worker startup, connects to each MCP server and caches
    its tool list. Workflow-side ``TemporalMCPClient.load_tools()`` reads from
    the cache. The plugin raises if any two clients share the same ``server``.
    """

    def __init__(
        self,
        *,
        model: TemporalModel | None = None,
        mcp_clients: list[TemporalMCPClient] = [],
    ) -> None:
        """Build the plugin from an optional model and MCP client list."""
        activities: list[Callable] = []
        if model is not None:
            ma = model._build_activity()
            activities.extend([ma.invoke_model, ma.invoke_model_streaming])

        names = [c.server for c in mcp_clients]
        if len(names) != len(set(names)):
            raise ValueError(
                "Duplicate MCP server names in mcp_clients; each must be unique."
            )
        for c in mcp_clients:
            activities.extend(c._get_activities())

        @asynccontextmanager
        async def run_context() -> AsyncGenerator[None, None]:
            for c in mcp_clients:
                await c._populate_cache()
            try:
                yield
            finally:
                for c in mcp_clients:
                    c._clear_cache()

        super().__init__(
            "aws.StrandsPlugin",
            workflow_runner=_workflow_runner,
            data_converter=_data_converter,
            activities=activities or None,
            run_context=run_context,
        )


def _workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
    if not runner:
        raise ValueError("No WorkflowRunner provided to the Strands plugin.")
    if isinstance(runner, SandboxedWorkflowRunner):
        return replace(
            runner,
            restrictions=runner.restrictions.with_passthrough_modules(
                "strands",
                "strands_tools",
                "mcp",
                "temporalio.contrib.strands",
                # The SDK's default passthrough already includes ``pydantic`` because
                # it lazy-imports inside some classes; extend that to its compiled
                # validation core and its ``Annotated`` helper for the same reason.
                "pydantic",
                "pydantic_core",
                "annotated_types",
            ),
        )
    return runner


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if (
        converter is None
        or converter.payload_converter_class is DefaultPayloadConverter
    ):
        return pydantic_data_converter
    return converter
