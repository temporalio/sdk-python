from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

from strands.models import BedrockModel, Model
from strands.sandbox import Sandbox
from strands.tools.mcp import MCPClient

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from ._failure_converter import StrandsFailureConverter
from ._model_activity import ModelActivity
from ._sandbox_activity import (
    SandboxActivities,
    SandboxWorkflowContext,
)
from ._temporal_mcp_client import (
    _evict_connection,
    build_call_tool_activity,
    build_list_tools_activity,
)


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    When ``models`` is supplied, registers a single pair of model invocation
    activities; each call carries the chosen ``model_name`` in its input and
    the worker resolves it against the factories. Factories are called lazily
    on first use, then cached for the worker's lifetime. Use the same name in
    ``TemporalAgent(model=...)`` inside the workflow.

    When ``mcp_clients`` is supplied, registers per-server
    ``{server}-call-tool`` and ``{server}-list-tools`` activities for each
    entry. Workflow-side ``TemporalMCPClient(server="...")`` discovers tools by
    running ``{server}-list-tools``; whether it lists once per workflow or once
    per agent turn is controlled by its ``cache_tools`` option.

    ``mcp_connection_idle_timeout`` controls how long a worker-process MCP
    connection is kept open between ``call-tool`` activities before it is
    disconnected; the timer resets on every reuse. Defaults to 5 minutes.

    When ``sandboxes`` is supplied, registers a stable set of name-prefixed
    activities for every sandbox factory. Each factory receives the owning
    Workflow chain's context and may return a sandbox directly or awaitably.
    Worker-local adapters are cached until ``sandbox_cache_idle_timeout``
    elapses. Use the same name in workflow-side ``TemporalSandbox(name)``
    instances.
    """

    def __init__(
        self,
        *,
        models: dict[str, Callable[[], Model]] | None = None,
        mcp_clients: dict[str, Callable[[], MCPClient]] | None = None,
        sandboxes: dict[
            str,
            Callable[
                [SandboxWorkflowContext],
                Sandbox | Awaitable[Sandbox],
            ],
        ]
        | None = None,
        mcp_connection_idle_timeout: timedelta | None = None,
        sandbox_cache_idle_timeout: timedelta | None = None,
    ) -> None:
        """Build the plugin from optional model, MCP, and sandbox factories.

        If ``models`` is omitted, registers a single ``BedrockModel()`` factory
        under the name ``"bedrock"``, matching Strands' own implicit default.
        """
        default_name: str | None = None
        if models is None:
            models = {"bedrock": lambda: BedrockModel()}
            default_name = "bedrock"
        activities: list[Callable] = []
        if models:
            ma = ModelActivity(models, default_name=default_name)
            activities.extend([ma.invoke_model, ma.invoke_model_streaming])

        sandbox_activity_groups = [
            SandboxActivities(name, sandbox_factory, sandbox_cache_idle_timeout)
            for name, sandbox_factory in (sandboxes or {}).items()
        ]
        for sandbox_activities in sandbox_activity_groups:
            activities.extend(sandbox_activities.activities())

        mcp_clients = mcp_clients or {}
        for server, client_factory in mcp_clients.items():
            activities.append(
                build_call_tool_activity(
                    server, client_factory, mcp_connection_idle_timeout
                )
            )
            activities.append(
                build_list_tools_activity(
                    server, client_factory, mcp_connection_idle_timeout
                )
            )

        @asynccontextmanager
        async def run_context() -> AsyncGenerator[None, None]:
            try:
                yield
            finally:
                for sandbox_activities in sandbox_activity_groups:
                    await sandbox_activities.aclose()
                for server in mcp_clients:
                    await _evict_connection(server)

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
