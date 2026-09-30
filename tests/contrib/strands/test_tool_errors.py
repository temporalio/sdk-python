from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolResult, ToolSpec

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.contrib.strands._temporal_mcp_client import _CallToolArgs, _MCPToolInfo
from temporalio.contrib.strands._temporal_mcp_tool import TemporalMCPTool
from temporalio.contrib.strands.workflow import activity_as_tool
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.mock_model import MockModel

_MODEL_TOOL_RESULTS: list[ToolResult] = []


class _RecordingModel(MockModel):
    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        for message in messages:
            for content in message["content"]:
                if "toolResult" in content:
                    _MODEL_TOOL_RESULTS.append(content["toolResult"])
        async for event in super().stream(
            messages,
            tool_specs,
            system_prompt,
            tool_choice=tool_choice,
            system_prompt_content=system_prompt_content,
            invocation_state=invocation_state,
            **kwargs,
        ):
            yield event


class _ToolResultHook(HookProvider):
    def __init__(self) -> None:
        self.failures: list[dict[str, str | None]] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(AfterToolCallEvent, self._record)

    def _record(self, event: AfterToolCallEvent) -> None:
        if event.result["status"] == "error":
            error = event.exception
            self.failures.append(
                {
                    "text": event.result["content"][0].get("text"),
                    "exception_type": type(error).__name__ if error else None,
                    "exception_message": error.message
                    if isinstance(error, ApplicationError)
                    else None,
                }
            )


@activity.defn
async def failing_activity_tool(location: str) -> None:
    raise ApplicationError(f"Unknown location: {location}", non_retryable=True)


@activity.defn(name="failing-mcp-call-tool")
async def failing_mcp_call_tool(_args: _CallToolArgs) -> None:
    raise ApplicationError("MCP server unavailable", non_retryable=True)


@workflow.defn
class _FailingToolWorkflow:
    @workflow.run
    async def run(self, kind: str) -> list[dict[str, str | None]]:
        if kind == "activity":
            tools = [
                activity_as_tool(
                    failing_activity_tool,
                    start_to_close_timeout=timedelta(seconds=15),
                )
            ]
        else:
            tools = [
                TemporalMCPTool(
                    "failing-mcp",
                    _MCPToolInfo("list_files", "List files", {"type": "object"}),
                    {"start_to_close_timeout": timedelta(seconds=15)},
                )
            ]
        hook = _ToolResultHook()
        agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
            tools=tools,
            hooks=[hook],
        )
        await agent.invoke_async("Use the tool")
        return hook.failures


@pytest.mark.parametrize(
    ("kind", "tool_name", "tool_input", "message"),
    [
        (
            "activity",
            "failing_activity_tool",
            {"location": "Atlantis"},
            "Unknown location: Atlantis",
        ),
        ("mcp", "list_files", {"path": "/"}, "MCP server unavailable"),
    ],
)
async def test_failed_activity_tool_reports_cause(
    client: Client,
    kind: str,
    tool_name: str,
    tool_input: dict[str, Any],
    message: str,
) -> None:
    _MODEL_TOOL_RESULTS.clear()
    task_queue = f"test_failed_tool-{uuid4()}"
    plugin = StrandsPlugin(
        models={
            "mock": lambda: _RecordingModel(
                [{"name": tool_name, "input": tool_input}, "Done!"]
            )
        }
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[_FailingToolWorkflow],
        activities=[failing_activity_tool, failing_mcp_call_tool],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            _FailingToolWorkflow.run,
            kind,
            id=f"test_failed_tool_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == [
            {
                "text": message,
                "exception_type": "ApplicationError",
                "exception_message": message,
            }
        ]

    assert len(_MODEL_TOOL_RESULTS) == 1
    assert _MODEL_TOOL_RESULTS[0]["status"] == "error"
    assert _MODEL_TOOL_RESULTS[0]["content"] == [{"text": message}]

    await Replayer(workflows=[_FailingToolWorkflow], plugins=[plugin]).replay_workflow(
        await handle.fetch_history()
    )
