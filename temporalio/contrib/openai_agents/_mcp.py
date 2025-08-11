import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, Callable, Optional, Sequence, Union

from agents import AgentBase, RunContextWrapper
from agents.mcp import MCPServer
from mcp import GetPromptResult, ListPromptsResult  # type:ignore
from mcp import Tool as MCPTool  # type:ignore
from mcp.types import CallToolResult  # type:ignore

from temporalio import activity, workflow
from temporalio.api.enums.v1.workflow_pb2 import (
    TIMEOUT_TYPE_HEARTBEAT,
    TIMEOUT_TYPE_SCHEDULE_TO_START,
)
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import PollerBehaviorSimpleMaximum, Worker
from temporalio.workflow import ActivityConfig, ActivityHandle

logger = logging.getLogger(__name__)


class StatelessTemporalMCPServer(MCPServer):
    def __init__(
        self, server: Union[MCPServer, str], config: Optional[ActivityConfig] = None
    ):
        self.server = server if isinstance(server, MCPServer) else None
        self._name = (server if isinstance(server, str) else server.name) + "-stateless"
        self.config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )
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
        server = self.server
        if server is None:
            raise ValueError(
                "A full MCPServer implementation should have been provided when adding a server to the worker."
            )

        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            try:
                await server.connect()
                return await server.list_tools()
            finally:
                await server.cleanup()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            try:
                await server.connect()
                return await server.call_tool(tool_name, arguments)
            finally:
                await server.cleanup()

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            try:
                await server.connect()
                return await server.list_prompts()
            finally:
                await server.cleanup()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            try:
                await server.connect()
                return await server.get_prompt(name, arguments)
            finally:
                await server.cleanup()

        return list_tools, call_tool, list_prompts, get_prompt


class StatefulTemporalMCPServer(MCPServer):
    def __init__(
        self,
        server: Union[MCPServer, str],
        config: Optional[ActivityConfig] = None,
        connect_config: Optional[ActivityConfig] = None,
    ):
        self.server = server if isinstance(server, MCPServer) else None
        self._name = (server if isinstance(server, str) else server.name) + "-stateful"
        self.config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1),
            schedule_to_start_timeout=timedelta(seconds=30),
        )
        self.connect_config = connect_config or ActivityConfig(
            start_to_close_timeout=timedelta(hours=1),
        )
        self._connect_handle: Optional[ActivityHandle] = None
        super().__init__()

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        self.config["task_queue"] = workflow.info().workflow_id + "-" + self.name
        self._connect_handle = workflow.start_activity(
            self.name + "-connect",
            args=[],
            **self.connect_config,
        )

    async def cleanup(self) -> None:
        if self._connect_handle:
            self._connect_handle.cancel()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        try:
            logger.info("Executing list-tools: %s", self.config)
            return await workflow.execute_activity(
                self.name + "-list-tools",
                args=[],
                result_type=list[MCPTool],
                **self.config,
            )
        except ActivityError as e:
            failure = e.failure
            if failure:
                cause = failure.cause
                if cause:
                    if (
                        cause.timeout_failure_info.timeout_type
                        == TIMEOUT_TYPE_SCHEDULE_TO_START
                    ):
                        raise ApplicationError(
                            "MCP Stateful Server Worker failed to schedule activity."
                        ) from e
                    if (
                        cause.timeout_failure_info.timeout_type
                        == TIMEOUT_TYPE_HEARTBEAT
                    ):
                        raise ApplicationError(
                            "MCP Stateful Server Worker failed to heartbeat."
                        ) from e
            raise e

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
        server = self.server
        if server is None:
            raise ValueError(
                "A full MCPServer implementation should have been provided when adding a server to the worker."
            )

        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            return await server.list_tools()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            return await server.call_tool(tool_name, arguments)

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            return await server.list_prompts()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            return await server.get_prompt(name, arguments)

        async def heartbeat_every(delay: float, *details: Any) -> None:
            """Heartbeat every so often while not cancelled"""
            while True:
                await asyncio.sleep(delay)
                activity.heartbeat(*details)

        @activity.defn(name=self.name + "-connect")
        async def connect() -> None:
            logger.info("Connect activity")
            heartbeat_task = asyncio.create_task(heartbeat_every(30))
            try:
                await server.connect()

                worker = Worker(
                    activity.client(),
                    task_queue=activity.info().workflow_id + "-" + self.name,
                    activities=[list_tools, call_tool, list_prompts, get_prompt],
                    activity_task_poller_behavior=PollerBehaviorSimpleMaximum(1),
                )

                await worker.run()
            finally:
                await server.cleanup()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        return (connect,)
