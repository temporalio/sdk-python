import abc
import asyncio
import functools
import logging
from contextlib import AbstractAsyncContextManager
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


class _StatelessMCPServerReference(MCPServer):
    def __init__(self, server: str, config: Optional[ActivityConfig] = None):
        self._name = server + "-stateless"
        self._config = config or ActivityConfig(
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
            **self._config,
        )

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        return await workflow.execute_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self._config,
        )

    async def list_prompts(self) -> ListPromptsResult:
        return await workflow.execute_activity(
            self.name + "-list-prompts",
            args=[],
            result_type=ListPromptsResult,
            **self._config,
        )

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        return await workflow.execute_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            result_type=GetPromptResult,
            **self._config,
        )


class StatelessMCPServer:
    """A stateless MCP server implementation for Temporal workflows.

    This class wraps an MCP server to make it stateless by executing each MCP operation
    as a separate Temporal activity. Each operation (list_tools, call_tool, etc.) will
    connect to the underlying server, execute the operation, and then clean up the connection.

    This approach will not maintain state across calls. If the desired MCPServer needs persistent state in order to
    function, this cannot be used.
    """

    def __init__(self, server: MCPServer):
        """Initialize the stateless temporal MCP server.

        Args:
            server: An MCPServer instance
        """
        self._server = server
        self._name = server.name + "-stateless"
        super().__init__()

    @property
    def name(self) -> str:
        """Get the server name."""
        return self._name

    def _get_activities(self) -> Sequence[Callable]:
        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            try:
                await self._server.connect()
                return await self._server.list_tools()
            finally:
                await self._server.cleanup()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            try:
                await self._server.connect()
                return await self._server.call_tool(tool_name, arguments)
            finally:
                await self._server.cleanup()

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            try:
                await self._server.connect()
                return await self._server.list_prompts()
            finally:
                await self._server.cleanup()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            try:
                await self._server.connect()
                return await self._server.get_prompt(name, arguments)
            finally:
                await self._server.cleanup()

        return list_tools, call_tool, list_prompts, get_prompt


def _handle_worker_failure(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
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
                            "MCP Stateful Server Worker failed to schedule activity.",
                            type="DedicatedWorkerFailure",
                        ) from e
                    if (
                        cause.timeout_failure_info.timeout_type
                        == TIMEOUT_TYPE_HEARTBEAT
                    ):
                        raise ApplicationError(
                            "MCP Stateful Server Worker failed to heartbeat.",
                            type="DedicatedWorkerFailure",
                        ) from e
            raise e

    return wrapper


class _StatefulMCPServerReference(MCPServer, AbstractAsyncContextManager):
    def __init__(
        self,
        server: str,
        config: Optional[ActivityConfig] = None,
        server_session_config: Optional[ActivityConfig] = None,
    ):
        self._name = server + "-stateful"
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1),
            schedule_to_start_timeout=timedelta(seconds=30),
        )
        self._server_session_config = server_session_config or ActivityConfig(
            start_to_close_timeout=timedelta(hours=1),
        )
        self._connect_handle: Optional[ActivityHandle] = None
        self._task_queue = f"{workflow.uuid4()}-{self.name}"
        self._config["task_queue"] = self._task_queue
        super().__init__()

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        self._connect_handle = workflow.start_activity(
            self.name + "-server-session",
            args=[self._task_queue],
            **self._server_session_config,
        )

    async def cleanup(self) -> None:
        pass
        # if self._connect_handle:
        #     self._connect_handle.cancel()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    @_handle_worker_failure
    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        return await workflow.execute_activity(
            self.name + "-list-tools",
            args=[],
            result_type=list[MCPTool],
            **self._config,
        )

    @_handle_worker_failure
    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        return await workflow.execute_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self._config,
        )

    @_handle_worker_failure
    async def list_prompts(self) -> ListPromptsResult:
        return await workflow.execute_activity(
            self.name + "-list-prompts",
            args=[],
            result_type=ListPromptsResult,
            **self._config,
        )

    @_handle_worker_failure
    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        return await workflow.execute_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            result_type=GetPromptResult,
            **self._config,
        )


class StatefulMCPServer:
    """A stateful MCP server implementation for Temporal workflows.

    This class wraps an MCP server to maintain a persistent connection throughout
    the workflow execution. It creates a dedicated worker that stays connected to
    the MCP server and processes operations on a dedicated task queue.

    This approach will allow the MCPServer to maintain state across calls if needed, but the caller
    will have to handle cases where the dedicated worker fails, as Temporal is unable to seamlessly
    recreate any lost state in that case. It is discouraged to use this approach unless necessary.

    Handling dedicated worker failure will entail catching ApplicationError with type "DedicatedWorkerFailure".
    Depending on the usage pattern, the caller will then have to either restart from the point at which the Stateful
    server was needed or handle continuing from that loss of state in some other way.
    """

    def __init__(
        self,
        server: MCPServer,
    ):
        """Initialize the stateful temporal MCP server.

        Args:
            server: Either an MCPServer instance or a string name for the server.
        """
        self._server = server
        self._name = self._server.name + "-stateful"
        self._connect_handle: Optional[ActivityHandle] = None
        super().__init__()

    @property
    def name(self) -> str:
        """Get the server name."""
        return self._name

    def _get_activities(self) -> Sequence[Callable]:
        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            return await self._server.list_tools()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            return await self._server.call_tool(tool_name, arguments)

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            return await self._server.list_prompts()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            return await self._server.get_prompt(name, arguments)

        async def heartbeat_every(delay: float, *details: Any) -> None:
            """Heartbeat every so often while not cancelled"""
            while True:
                await asyncio.sleep(delay)
                activity.heartbeat(*details)

        @activity.defn(name=self._name + "-server-session")
        async def connect(task_queue: str) -> None:
            heartbeat_task = asyncio.create_task(heartbeat_every(300))
            try:
                await self._server.connect()

                worker = Worker(
                    activity.client(),
                    task_queue=task_queue,
                    activities=[list_tools, call_tool, list_prompts, get_prompt],
                    activity_task_poller_behavior=PollerBehaviorSimpleMaximum(100),
                )

                await worker.run()
            finally:
                print("Cleanup")
                await self._server.cleanup()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        return (connect,)
