from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import EventActions
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from google.genai.types import FunctionDeclaration

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig


@dataclass
class _GetToolsArguments:
    factory_argument: Any | None


@dataclass
class _ToolResult:
    name: str
    description: str
    is_long_running: bool
    custom_metadata: dict[str, Any] | None
    function_declaration: FunctionDeclaration | None


@dataclass
class TemporalToolContext:
    """Context for tools running within Temporal workflows.
    
    Provides access to tool confirmation and event actions for ADK integration.
    """
    tool_confirmation: ToolConfirmation | None
    function_call_id: str | None
    event_actions: EventActions

    def request_confirmation(
        self,
        *,
        hint: str | None = None,
        payload: Any | None = None,
    ) -> None:
        """Requests confirmation for the given function call.

        Args:
          hint: A hint to the user on how to confirm the tool call.
          payload: The payload used to confirm the tool call.
        """
        if not self.function_call_id:
            raise ValueError("function_call_id is not set.")
        self.event_actions.requested_tool_confirmations[self.function_call_id] = (
            ToolConfirmation(
                hint=hint or "",
                payload=payload,
            )
        )


@dataclass
class _CallToolResult:
    result: Any
    tool_context: TemporalToolContext


@dataclass
class _CallToolArguments:
    factory_argument: Any | None
    name: str
    arguments: dict[str, Any]
    tool_context: TemporalToolContext


class TemporalMcpToolSetProvider:
    """Provider for creating Temporal-aware MCP toolsets.
    
    Manages the creation of toolset activities and handles tool execution
    within Temporal workflows.
    """
    def __init__(self, name: str, toolset_factory: Callable[[Any | None], McpToolset]):
        """Initializes the toolset provider.

        Args:
            name: Name prefix for the generated activities.
            toolset_factory: Factory function that creates McpToolset instances.
        """
        super().__init__()
        self._name = name
        self._toolset_factory = toolset_factory

    def _get_activities(self) -> Sequence[Callable]:
        @activity.defn(name=self._name + "-list-tools")
        async def get_tools(
            args: _GetToolsArguments,
        ) -> list[_ToolResult]:
            toolset = self._toolset_factory(args.factory_argument)
            tools = await toolset.get_tools()
            return [
                _ToolResult(
                    tool.name,
                    tool.description,
                    tool.is_long_running,
                    tool.custom_metadata,
                    tool._get_declaration(),
                )
                for tool in tools
            ]

        @activity.defn(name=self._name + "-call-tool")
        async def call_tool(
            args: _CallToolArguments,
        ) -> _CallToolResult:
            toolset = self._toolset_factory(args.factory_argument)
            tools = await toolset.get_tools()
            tool_match = [tool for tool in tools if tool.name == args.name]
            if len(tool_match) == 0:
                raise ApplicationError(
                    f"Unable to find matching mcp tool by name: {args.name}"
                )
            if len(tool_match) > 1:
                raise ApplicationError(
                    f"Unable too many matching mcp tools by name: {args.name}"
                )
            tool = tool_match[0]

            # We cannot provide a full-fledged ToolContext so we need to provide only what is needed by the tool
            result = await tool.run_async(
                args=args.arguments,
                tool_context=args.tool_context,  #  type:ignore
            )
            return _CallToolResult(result=result, tool_context=args.tool_context)

        return get_tools, call_tool


class _TemporalTool(BaseTool):
    def __init__(
        self,
        set_name: str,
        factory_argument: Any | None,
        config: ActivityConfig | None,
        declaration: FunctionDeclaration | None,
        *,
        name: str,
        description: str,
        is_long_running: bool = False,
        custom_metadata: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            description=description,
            is_long_running=is_long_running,
            custom_metadata=custom_metadata,
        )
        self._set_name = set_name
        self._factory_argument = factory_argument
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )
        self._declaration = declaration

    def _get_declaration(self) -> types.FunctionDeclaration | None:
        return self._declaration

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        result: _CallToolResult = await workflow.execute_activity(
            self._set_name + "-call-tool",
            _CallToolArguments(
                self._factory_argument,
                self.name,
                arguments=args,
                tool_context=TemporalToolContext(
                    tool_confirmation=tool_context.tool_confirmation,
                    function_call_id=tool_context.function_call_id,
                    event_actions=tool_context._event_actions,
                ),
            ),
            result_type=_CallToolResult,
            **self._config,
        )

        # We need to propagate any event actions back to the main context
        tool_context._event_actions = result.tool_context.event_actions
        return result.result


class TemporalMcpToolSet(BaseToolset):
    """Temporal-aware MCP toolset implementation.
    
    Executes MCP tools as Temporal activities, providing proper isolation
    and execution guarantees within workflows.
    """
    def __init__(
        self,
        name: str,
        config: ActivityConfig | None = None,
        factory_argument: Any | None = None,
    ):
        """Initializes the Temporal MCP toolset.

        Args:
            name: Name of the toolset (used for activity naming).
            config: Optional activity configuration.
            factory_argument: Optional argument passed to toolset factory.
        """
        super().__init__()
        self._name = name
        self._factory_argument = factory_argument
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        """Retrieves available tools from the MCP toolset.

        Args:
            readonly_context: Optional readonly context (unused in this implementation).

        Returns:
            List of available tools wrapped as Temporal activities.
        """
        tool_results: list[_ToolResult] = await workflow.execute_activity(
            self._name + "-list-tools",
            _GetToolsArguments(self._factory_argument),
            result_type=list[_ToolResult],
            **self._config,
        )
        return [
            _TemporalTool(
                set_name=self._name,
                factory_argument=self._factory_argument,
                config=self._config,
                declaration=tool_result.function_declaration,
                name=tool_result.name,
                description=tool_result.description,
                is_long_running=tool_result.is_long_running,
                custom_metadata=tool_result.custom_metadata,
            )
            for tool_result in tool_results
        ]
