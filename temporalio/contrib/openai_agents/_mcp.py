from datetime import timedelta
from typing import Any, Callable, Optional, Sequence

from agents import AgentBase, RunContextWrapper
from agents.mcp import MCPServer
from mcp import GetPromptResult, ListPromptsResult
from mcp import Tool as MCPTool
from mcp.types import CallToolResult

from temporalio import activity, workflow


class TemporalMCPServerWorkflowShim(MCPServer):
    def __init__(self, name: str):
        self.server_name = name
        super().__init__()

    @property
    def name(self) -> str:
        return self.server_name

    async def connect(self) -> None:
        raise ValueError("Cannot connect to a server shim")

    async def cleanup(self) -> None:
        raise ValueError("Cannot clean up a server shim")

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        workflow.logger.info("Listing tools")
        tools: list[MCPTool] = await workflow.execute_local_activity(
            self.name + "-list-tools",
            start_to_close_timeout=timedelta(seconds=30),
            result_type=list[MCPTool],
        )
        print(tools[0])
        print("Tool type:", type(tools[0]))
        # print(type(MCPTool(**tools[0])))
        return tools

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        return await workflow.execute_local_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            start_to_close_timeout=timedelta(seconds=30),
            result_type=CallToolResult,
        )

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError()

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        raise NotImplementedError()


class TemporalMCPServer(TemporalMCPServerWorkflowShim):
    def __init__(self, server: MCPServer):
        self.server = server
        super().__init__(server.name)

    @property
    def name(self) -> str:
        return self.server.name

    async def connect(self) -> None:
        await self.server.connect()

    async def cleanup(self) -> None:
        await self.server.cleanup()

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        if not workflow.in_workflow():
            return await self.server.list_tools(run_context, agent)

        return await super().list_tools(run_context, agent)

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        if not workflow.in_workflow():
            return await self.server.call_tool(tool_name, arguments)

        return await super().call_tool(tool_name, arguments)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    def get_activities(self) -> Sequence[Callable]:
        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            activity.logger.info("Listing tools in activity")
            return await self.server.list_tools()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            return await self.server.call_tool(tool_name, arguments)

        return list_tools, call_tool
