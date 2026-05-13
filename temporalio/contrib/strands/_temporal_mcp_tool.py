from typing import Any

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

from temporalio import workflow

from ._temporal_mcp_client import _CallToolArgs, _MCPToolInfo


class TemporalMCPTool(AgentTool):
    """Workflow-side stub for a single MCP tool; dispatches to an activity."""

    def __init__(
        self,
        server: str,
        info: _MCPToolInfo,
        options: dict[str, Any],
    ) -> None:
        super().__init__()
        self._server = server
        self._info = info
        self._options = options

    @property
    def tool_name(self) -> str:
        return self._info.name

    @property
    def tool_spec(self) -> ToolSpec:
        spec: ToolSpec = {
            "name": self._info.name,
            "description": self._info.description
            or f"Tool which performs {self._info.name}",
            "inputSchema": {"json": self._info.input_schema},
        }
        if self._info.output_schema:
            spec["outputSchema"] = {"json": self._info.output_schema}
        return spec

    @property
    def tool_type(self) -> str:
        return "temporal_mcp"

    async def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        result: ToolResult = await workflow.execute_activity(
            f"{self._server}-call-tool",
            _CallToolArgs(
                tool_name=self._info.name,
                arguments=tool_use["input"],
                tool_use_id=tool_use["toolUseId"],
            ),
            **self._options,
        )
        yield ToolResultEvent(result)
