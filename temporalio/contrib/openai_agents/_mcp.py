from datetime import timedelta
from typing import Any, Callable, Optional, Sequence, Union

from agents import AgentBase, RunContextWrapper
from agents.mcp import MCPServer
from mcp import GetPromptResult, ListPromptsResult  # type:ignore
from mcp import Tool as MCPTool  # type:ignore
from mcp.types import CallToolResult  # type:ignore

from temporalio import activity, workflow
from temporalio.workflow import ActivityConfig

class StatelessTemporalMCPServer(MCPServer):
    def __init__(self, server: Union[MCPServer, str], config: Optional[ActivityConfig] = None):
        self.server = server if isinstance(server, MCPServer) else None
        self._name = (server if isinstance(server, str) else server.name) + "-stateless"
        self.config = config or ActivityConfig(start_to_close_timeout=timedelta(minutes=1))
        super().__init__()

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        return await workflow.execute_activity(
            self.name + "-list-tools",
            args=[],
            result_type=list[MCPTool],
            **self.config,
        )

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        return await workflow.execute_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self.config,
        )

    async def list_prompts(self) -> ListPromptsResult:
        return await workflow.execute_activity(
            self.name + "-list-prompts",
            args=[],
            result_type=ListPromptsResult,
            **self.config,
        )

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        return await workflow.execute_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            result_type=GetPromptResult,
            **self.config,
        )

    def get_activities(self) -> Sequence[Callable]:
        if self.server is None:
            raise ValueError("A full MCPServer implementation should have been provided when adding a server to the worker.")

        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            activity.logger.info("Listing tools in activity")
            async with self.server:
                return await self.server.list_tools()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            async with self.server:
                return await self.server.call_tool(tool_name, arguments)

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            async with self.server:
                return await self.server.list_prompts()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            async with self.server:
                return await self.server.get_prompt(name, arguments)

        return list_tools, call_tool, list_prompts, get_prompt
