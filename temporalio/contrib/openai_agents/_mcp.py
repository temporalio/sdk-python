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
    """A stateless MCP server implementation for Temporal workflows.

    This class wraps an MCP server to make it stateless by executing each MCP operation
    as a separate Temporal activity. Each operation (list_tools, call_tool, etc.) will
    connect to the underlying server, execute the operation, and then clean up the connection.

    This approach is suitable for simple use cases where connection overhead is acceptable
    and you don't need to maintain state between operations.
    """

    def __init__(
        self, server: Union[MCPServer, str], config: Optional[ActivityConfig] = None
    ):
        """Initialize the stateless temporal MCP server.

        Args:
            server: Either an MCPServer instance or a string name for the server.
            config: Optional activity configuration for Temporal activities. Defaults to
                   1-minute start-to-close timeout if not provided.
        """
        self.server = server if isinstance(server, MCPServer) else None
        self._name = (server if isinstance(server, str) else server.name) + "-stateless"
        self.config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )
        super().__init__()

    @property
    def name(self) -> str:
        """Get the server name with '-stateless' suffix.

        Returns:
            The server name with '-stateless' appended.
        """
        return self._name

    async def connect(self) -> None:
        """Connect to the MCP server.

        For stateless servers, this is a no-op since connections are made
        on a per-operation basis.
        """
        pass

    async def cleanup(self) -> None:
        """Clean up the MCP server connection.

        For stateless servers, this is a no-op since connections are cleaned
        up after each operation.
        """
        pass

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        """List available tools from the MCP server.

        This method executes a Temporal activity to connect to the MCP server,
        retrieve the list of available tools, and clean up the connection.

        Args:
            run_context: Optional run context wrapper (unused in stateless mode).
            agent: Optional agent base (unused in stateless mode).

        Returns:
            A list of available MCP tools.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-list-tools",
            args=[],
            result_type=list[MCPTool],
            **self.config,
        )

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        """Call a specific tool on the MCP server.

        This method executes a Temporal activity to connect to the MCP server,
        call the specified tool with the given arguments, and clean up the connection.

        Args:
            tool_name: The name of the tool to call.
            arguments: Optional dictionary of arguments to pass to the tool.

        Returns:
            The result of the tool call.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self.config,
        )

    async def list_prompts(self) -> ListPromptsResult:
        """List available prompts from the MCP server.

        This method executes a Temporal activity to connect to the MCP server,
        retrieve the list of available prompts, and clean up the connection.

        Returns:
            A list of available prompts.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-list-prompts",
            args=[],
            result_type=ListPromptsResult,
            **self.config,
        )

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        """Get a specific prompt from the MCP server.

        This method executes a Temporal activity to connect to the MCP server,
        retrieve the specified prompt with optional arguments, and clean up the connection.

        Args:
            name: The name of the prompt to retrieve.
            arguments: Optional dictionary of arguments for the prompt.

        Returns:
            The prompt result.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            result_type=GetPromptResult,
            **self.config,
        )

    def get_activities(self) -> Sequence[Callable]:
        """Get the Temporal activities for this MCP server.

        Creates and returns the Temporal activity functions that handle MCP operations.
        Each activity manages its own connection lifecycle (connect -> operate -> cleanup).

        Returns:
            A sequence of Temporal activity functions.

        Raises:
            ValueError: If no MCP server instance was provided during initialization.
        """
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
    """A stateful MCP server implementation for Temporal workflows.

    This class wraps an MCP server to maintain a persistent connection throughout
    the workflow execution. It creates a dedicated worker that stays connected to
    the MCP server and processes operations on a dedicated task queue.

    This approach is more efficient for workflows that make multiple MCP calls,
    as it avoids connection overhead, but requires more resources to maintain
    the persistent connection and worker.

    The caller will have to handle cases where the dedicated worker fails, as Temporal is
    unable to seamlessly recreate any lost state in that case.
    """

    def __init__(
        self,
        server: Union[MCPServer, str],
        config: Optional[ActivityConfig] = None,
        connect_config: Optional[ActivityConfig] = None,
    ):
        """Initialize the stateful temporal MCP server.

        Args:
            server: Either an MCPServer instance or a string name for the server.
            config: Optional activity configuration for MCP operation activities.
                   Defaults to 1-minute start-to-close and 30-second schedule-to-start timeouts.
            connect_config: Optional activity configuration for the connection activity.
                           Defaults to 1-hour start-to-close timeout.
        """
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
        """Get the server name with '-stateful' suffix.

        Returns:
            The server name with '-stateful' appended.
        """
        return self._name

    async def connect(self) -> None:
        """Connect to the MCP server and start the dedicated worker.

        This method creates a dedicated task queue for this workflow and starts
        a long-running activity that maintains the connection and runs a worker
        to handle MCP operations.
        """
        self.config["task_queue"] = workflow.info().workflow_id + "-" + self.name
        self._connect_handle = workflow.start_activity(
            self.name + "-connect",
            args=[],
            **self.connect_config,
        )

    async def cleanup(self) -> None:
        """Clean up the MCP server connection.

        This method cancels the long-running connection activity, which will
        cause the dedicated worker to shut down and the MCP server connection
        to be closed.
        """
        if self._connect_handle:
            self._connect_handle.cancel()

    async def __aenter__(self):
        """Async context manager entry point.

        Returns:
            This server instance after connecting.
        """
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """Async context manager exit point.

        Args:
            exc_type: Exception type if an exception occurred.
            exc_value: Exception value if an exception occurred.
            traceback: Exception traceback if an exception occurred.
        """
        await self.cleanup()

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        """List available tools from the MCP server.

        This method executes a Temporal activity on the dedicated task queue
        to retrieve the list of available tools from the persistent MCP connection.

        Args:
            run_context: Optional run context wrapper (unused in stateful mode).
            agent: Optional agent base (unused in stateful mode).

        Returns:
            A list of available MCP tools.

        Raises:
            ApplicationError: If the MCP worker fails to schedule or heartbeat.
            ActivityError: If the underlying Temporal activity fails.
        """
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
        """Call a specific tool on the MCP server.

        This method executes a Temporal activity on the dedicated task queue
        to call the specified tool using the persistent MCP connection.

        Args:
            tool_name: The name of the tool to call.
            arguments: Optional dictionary of arguments to pass to the tool.

        Returns:
            The result of the tool call.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self.config,
        )

    async def list_prompts(self) -> ListPromptsResult:
        """List available prompts from the MCP server.

        This method executes a Temporal activity on the dedicated task queue
        to retrieve the list of available prompts from the persistent MCP connection.

        Returns:
            A list of available prompts.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-list-prompts",
            args=[],
            result_type=ListPromptsResult,
            **self.config,
        )

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        """Get a specific prompt from the MCP server.

        This method executes a Temporal activity on the dedicated task queue
        to retrieve the specified prompt using the persistent MCP connection.

        Args:
            name: The name of the prompt to retrieve.
            arguments: Optional dictionary of arguments for the prompt.

        Returns:
            The prompt result.

        Raises:
            ActivityError: If the underlying Temporal activity fails.
        """
        return await workflow.execute_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            result_type=GetPromptResult,
            **self.config,
        )

    def get_activities(self) -> Sequence[Callable]:
        """Get the Temporal activities for this stateful MCP server.

        Creates and returns the Temporal activity functions that handle MCP operations
        and connection management. This includes a long-running connect activity that
        maintains the MCP connection and runs a dedicated worker.

        Returns:
            A sequence containing the connect activity function.

        Raises:
            ValueError: If no MCP server instance was provided during initialization.
        """
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
