from datetime import timedelta
from typing import Any, Callable, Optional, Sequence

from agents import AgentBase, RunContextWrapper
from agents.mcp import MCPServer
from mcp import GetPromptResult, ListPromptsResult
from mcp import Tool as MCPTool
from mcp.types import CallToolResult

from temporalio import activity, workflow
from temporalio.workflow import LocalActivityConfig


class TemporalMCPServerWorkflowShim(MCPServer):
    """Workflow-compatible shim for MCP (Model Context Protocol) servers.

    This class provides a Temporal workflow-safe interface for interacting with MCP servers.
    Instead of directly connecting to external MCP servers (which would violate workflow
    determinism), this shim delegates tool operations to Temporal activities that can
    safely perform non-deterministic operations.

    The shim is designed to be used within Temporal workflows where direct network
    communication with external services is not allowed. It converts MCP server
    operations into local activity executions that maintain workflow determinism.

    Args:
        name: A descriptive name for the MCP server being shimmed.
        config: Optional LocalActivityConfig for customizing activity execution timeouts
            and other settings. Defaults to 1 minute timeout if not provided.

    Note:
        This is a base shim class that cannot actually connect to real MCP servers.
        Use TemporalMCPServer for wrapping actual MCP server instances.
    """

    def __init__(self, name: str, *, config: Optional[LocalActivityConfig] = None):
        """Initialize the MCP server workflow shim.

        Args:
            name: A descriptive name for the MCP server being shimmed.
            config: Optional LocalActivityConfig for customizing activity execution.
                If not provided, defaults to a 1-minute start_to_close_timeout.
        """
        self.server_name = name
        self.config = config or LocalActivityConfig(
            start_to_close_timeout=timedelta(minutes=1),
        )
        super().__init__()

    @property
    def name(self) -> str:
        """Get the name of the MCP server.

        Returns:
            The descriptive name of the MCP server being shimmed.
        """
        return self.server_name

    async def connect(self) -> None:
        """Attempt to connect to the MCP server.

        Raises:
            ValueError: Always raised as this shim cannot connect to real servers.
                Use TemporalMCPServer for actual server connections.
        """
        raise ValueError("Cannot connect to a server shim")

    async def cleanup(self) -> None:
        """Attempt to clean up the MCP server connection.

        Raises:
            ValueError: Always raised as this shim cannot clean up real servers.
                Use TemporalMCPServer for actual server cleanup.
        """
        raise ValueError("Cannot clean up a server shim")

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        """List available tools from the MCP server via Temporal activity.

        This method executes a local activity to retrieve the list of available tools
        from the underlying MCP server. The activity execution ensures workflow
        determinism while allowing the actual tool listing to occur outside the
        workflow sandbox.

        Args:
            run_context: Optional runtime context wrapper for the operation.
            agent: Optional agent instance that may be relevant to tool listing.

        Returns:
            A list of MCP tools available from the server.

        Note:
            This method must be called from within a Temporal workflow context.
        """
        workflow.logger.info("Listing tools")
        tools: list[MCPTool] = await workflow.execute_local_activity(
            self.name + "-list-tools",
            result_type=list[MCPTool],
            **self.config,
        )
        return tools

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        """Execute a tool call via Temporal activity.

        This method executes a local activity to call the specified tool on the
        underlying MCP server. The activity execution ensures workflow determinism
        while allowing the actual tool execution to occur outside the workflow sandbox.

        Args:
            tool_name: The name of the tool to execute.
            arguments: Optional dictionary of arguments to pass to the tool.

        Returns:
            The result of the tool execution.

        Note:
            This method must be called from within a Temporal workflow context.
        """
        return await workflow.execute_local_activity(
            self.name + "-call-tool",
            args=[tool_name, arguments],
            result_type=CallToolResult,
            **self.config,
        )

    async def list_prompts(self) -> ListPromptsResult:
        """List available prompts from the MCP server via Temporal activity.

        This method executes a local activity to retrieve the list of available prompts
        from the underlying MCP server. The activity execution ensures workflow
        determinism while allowing the actual prompt listing to occur outside the
        workflow sandbox.

        Returns:
            A list of available prompts from the server.

        Note:
            This method must be called from within a Temporal workflow context.
        """
        return await workflow.execute_local_activity(
            self.name + "-list-prompts",
            result_type=ListPromptsResult,
            **self.config,
        )

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        """Get a specific prompt from the MCP server via Temporal activity.

        This method executes a local activity to retrieve a specific prompt from the
        underlying MCP server. The activity execution ensures workflow determinism
        while allowing the actual prompt retrieval to occur outside the workflow sandbox.

        Args:
            name: The name of the prompt to retrieve.
            arguments: Optional arguments for the prompt.

        Returns:
            The requested prompt from the server.

        Note:
            This method must be called from within a Temporal workflow context.
        """
        return await workflow.execute_local_activity(
            self.name + "-get-prompt",
            args=[name, arguments],
            **self.config,
        )


class TemporalMCPServer(TemporalMCPServerWorkflowShim):
    """Concrete implementation of a Temporal-compatible MCP server wrapper.
    
    This class only needs to be used if you want to manage the server lifetime manually.
    Otherwise, you can simply provide an MCPServer to the OpenAIAgentsPlugin, which will
    bind the server to the lifetime of the worker.

    This class wraps an actual MCP server instance and provides both workflow-safe
    and non-workflow execution modes. When used outside of a workflow context,
    it delegates directly to the wrapped server. When used within a workflow,
    it delegates to the parent shim class which executes operations via Temporal
    activities to maintain determinism.
    
    This dual-mode operation allows the same server instance to be used both
    in workflow and non-workflow contexts, making it convenient for testing
    and mixed-mode applications.
    
    The class also provides activity definitions that can be registered with
    a Temporal worker to enable the workflow-safe operations.
    
    Args:
        server: The actual MCP server instance to wrap.
    """
    
    def __init__(self, server: MCPServer):
        """Initialize the Temporal MCP server wrapper.
        
        Args:
            server: The actual MCP server instance to wrap and delegate to.
        """
        self.server = server
        super().__init__(server.name)

    @property
    def name(self) -> str:
        """Get the name of the wrapped MCP server.
        
        Returns:
            The name of the underlying MCP server instance.
        """
        return self.server.name

    async def connect(self) -> None:
        """Connect to the underlying MCP server.
        
        Delegates the connection operation to the wrapped server instance.
        This method can be called directly when not in a workflow context.
        """
        await self.server.connect()

    async def cleanup(self) -> None:
        """Clean up the underlying MCP server connection.
        
        Delegates the cleanup operation to the wrapped server instance.
        This method can be called directly when not in a workflow context.
        """
        await self.server.cleanup()

    async def list_tools(
        self,
        run_context: Optional[RunContextWrapper[Any]] = None,
        agent: Optional[AgentBase] = None,
    ) -> list[MCPTool]:
        """List available tools with context-aware execution.
        
        When called outside a workflow context, delegates directly to the wrapped
        server. When called within a workflow, delegates to the parent shim class
        which executes the operation via a Temporal activity.
        
        Args:
            run_context: Optional runtime context wrapper for the operation.
            agent: Optional agent instance that may be relevant to tool listing.
            
        Returns:
            A list of MCP tools available from the server.
        """
        if not workflow.in_workflow():
            return await self.server.list_tools(run_context, agent)

        return await super().list_tools(run_context, agent)

    async def call_tool(
        self, tool_name: str, arguments: Optional[dict[str, Any]]
    ) -> CallToolResult:
        """Execute a tool with context-aware execution.
        
        When called outside a workflow context, delegates directly to the wrapped
        server. When called within a workflow, delegates to the parent shim class
        which executes the operation via a Temporal activity.
        
        Args:
            tool_name: The name of the tool to execute.
            arguments: Optional dictionary of arguments to pass to the tool.
            
        Returns:
            The result of the tool execution.
        """
        if not workflow.in_workflow():
            return await self.server.call_tool(tool_name, arguments)

        return await super().call_tool(tool_name, arguments)

    async def __aenter__(self):
        """Async context manager entry - connects to the MCP server.
        
        Returns:
            Self, allowing the server to be used within the context.
        """
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """Async context manager exit - cleans up the MCP server connection.
        
        Args:
            exc_type: Exception type, if any exception occurred.
            exc_value: Exception value, if any exception occurred.
            traceback: Exception traceback, if any exception occurred.
        """
        await self.cleanup()

    def get_activities(self) -> Sequence[Callable]:
        """Get Temporal activity functions for workflow-safe MCP operations.
        
        This method creates and returns Temporal activity functions that delegate
        to the wrapped MCP server. These activities should be registered with a
        Temporal worker to enable workflow-safe execution of MCP operations.
        
        The returned activities include:
        - {name}-list-tools: Lists available tools
        - {name}-call-tool: Executes a specific tool
        - {name}-list-prompts: Lists available prompts
        - {name}-get-prompt: Retrieves a specific prompt
        
        Returns:
            A sequence of activity functions to register with a Temporal worker.
            
        Example:
            ```python
            async with TemporalMCPServer(my_mcp_server) as server:
                async with Worker(
                    client,
                    activities=server.get_activities(),
                ) as worker:
                    ...
            ```
        """
        @activity.defn(name=self.name + "-list-tools")
        async def list_tools() -> list[MCPTool]:
            activity.logger.info("Listing tools in activity")
            return await self.server.list_tools()

        @activity.defn(name=self.name + "-call-tool")
        async def call_tool(
            tool_name: str, arguments: Optional[dict[str, Any]]
        ) -> CallToolResult:
            return await self.server.call_tool(tool_name, arguments)

        @activity.defn(name=self.name + "-list-prompts")
        async def list_prompts() -> ListPromptsResult:
            return await self.server.list_prompts()

        @activity.defn(name=self.name + "-get-prompt")
        async def get_prompt(
            name: str, arguments: Optional[dict[str, Any]]
        ) -> GetPromptResult:
            return await self.server.get_prompt(name, arguments)

        return list_tools, call_tool, list_prompts, get_prompt

    async def list_prompts(self) -> ListPromptsResult:
        """List available prompts with context-aware execution.
        
        When called outside a workflow context, delegates directly to the wrapped
        server. When called within a workflow, delegates to the parent shim class
        which executes the operation via a Temporal activity.
        
        Returns:
            A list of available prompts from the server.
        """
        if not workflow.in_workflow():
            return await self.server.list_prompts()

        return await super().list_prompts()

    async def get_prompt(
        self, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> GetPromptResult:
        """Get a specific prompt with context-aware execution.
        
        When called outside a workflow context, delegates directly to the wrapped
        server. When called within a workflow, delegates to the parent shim class
        which executes the operation via a Temporal activity.
        
        Args:
            name: The name of the prompt to retrieve.
            arguments: Optional arguments for the prompt.
            
        Returns:
            The requested prompt from the server.
        """
        if not workflow.in_workflow():
            return await self.server.get_prompt(name, arguments)

        return await super().get_prompt(name, arguments)
