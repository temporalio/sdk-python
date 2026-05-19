from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import strands.models.model as _strands_model
from strands.models import Model
from strands.tools.mcp.mcp_types import MCPTransport

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from ._failure_converter import StrandsFailureConverter
from ._model_activity import ModelActivity
from ._temporal_mcp_client import (
    build_call_tool_activity,
    clear_cache,
    populate_cache,
)

# Force Strands' base Model.count_tokens to avoid tiktoken, which lazily downloads
# an encoding file. Use the default chars-per-token heuristic instead (deterministic).
setattr(_strands_model, "_get_encoding", lambda: None)


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    When ``models`` is supplied, registers a single pair of model invocation
    activities; each call carries the chosen ``model_name`` in its input and
    the worker resolves it against the factories. Factories are called lazily
    on first use, then cached for the worker's lifetime. Use the same name in
    ``TemporalAgent(model=...)`` inside the workflow.

    When ``mcp_clients`` is supplied, registers a per-server
    ``{server}-call-tool`` activity for each entry and, at worker startup,
    connects to each MCP server to cache its tool list. Workflow-side
    ``TemporalMCPClient(server="...").load_tools()`` reads from the cache.
    """

    def __init__(
        self,
        *,
        models: dict[str, Callable[[], Model]] | None = None,
        mcp_clients: dict[str, Callable[[], MCPTransport]] | None = None,
    ) -> None:
        """Build the plugin from optional model and MCP transport factories."""
        activities: list[Callable] = []
        if models:
            ma = ModelActivity(models)
            activities.extend([ma.invoke_model, ma.invoke_model_streaming])

        mcp_clients = mcp_clients or {}
        for server, transport_factory in mcp_clients.items():
            activities.append(build_call_tool_activity(server, transport_factory))

        @asynccontextmanager
        async def run_context() -> AsyncGenerator[None, None]:
            for server, transport_factory in mcp_clients.items():
                await populate_cache(server, transport_factory)
            try:
                yield
            finally:
                for server in mcp_clients:
                    clear_cache(server)

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
                # ``pydantic`` is already in the SDK default passthrough; extend it
                # to its compiled validation core and ``Annotated`` helper.
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
        return replace(
            pydantic_data_converter,
            failure_converter_class=StrandsFailureConverter,
        )
    return converter
