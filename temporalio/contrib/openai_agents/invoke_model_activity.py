"""A temporal activity that invokes a LLM model.

Implements mapping of OpenAI datastructures to Pydantic friendly types.
"""

import enum
import json
from dataclasses import dataclass
from typing import Any, Optional, Required, TypedDict, Union, cast

from agents import (
    AgentOutputSchemaBase,
    ComputerTool,
    FileSearchTool,
    FunctionTool,
    Handoff,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    RunContextWrapper,
    Tool,
    TResponseInputItem,
    UserError,
    WebSearchTool,
)
from agents.models.multi_provider import MultiProvider

from temporalio import activity
from temporalio.contrib.openai_agents._heartbeat_decorator import _auto_heartbeater


@dataclass
class HandoffInput:
    """Data conversion friendly representation of a Handoff."""

    tool_name: str
    tool_description: str
    input_json_schema: dict[str, Any]
    agent_name: str
    # input_filter: HandoffInputFilter | None = None
    strict_json_schema: bool = True


@dataclass
class FunctionToolInput:
    """Data conversion friendly representation of a FunctionTool."""

    name: str
    description: str
    params_json_schema: dict[str, Any]
    # on_invoke_tool: Callable[[RunContextWrapper[Any], str], Awaitable[Any]]
    strict_json_schema: bool = True


ToolInput = Union[FunctionToolInput, FileSearchTool, WebSearchTool]


@dataclass
class AgentOutputSchemaInput(AgentOutputSchemaBase):
    """Data conversion friendly representation of AgentOutputSchema."""

    output_type_name: str | None
    is_wrapped: bool
    output_schema: dict[str, Any] | None
    strict_json_schema: bool

    def is_plain_text(self) -> bool:
        """Whether the output type is plain text (versus a JSON object)."""
        return self.output_type_name is None or self.output_type_name == "str"

    def is_strict_json_schema(self) -> bool:
        """Whether the JSON schema is in strict mode."""
        return self.strict_json_schema

    def json_schema(self) -> dict[str, Any]:
        """The JSON schema of the output type."""
        if self.is_plain_text():
            raise UserError("Output type is plain text, so no JSON schema is available")
        if self.output_schema is None:
            raise UserError("Output schema is not defined")
        return self.output_schema

    def validate_json(self, json_str: str) -> Any:
        """Validate the JSON string against the schema."""
        raise NotImplementedError()

    def name(self) -> str:
        """Get the name of the output type."""
        if self.output_type_name is None:
            raise ValueError("output_type_name is None")
        return self.output_type_name


class ModelTracingInput(enum.IntEnum):
    """Conversion friendly representation of ModelTracing.

    Needed as ModelTracing is enum.Enum instead of IntEnum
    """

    DISABLED = 0
    ENABLED = 1
    ENABLED_WITHOUT_DATA = 2


class ActivityModelInput(TypedDict, total=False):
    """Input for the invoke_model_activity activity."""

    model_name: Optional[str]
    system_instructions: Optional[str]
    input: Required[str | list[TResponseInputItem]]
    model_settings: Required[ModelSettings]
    tools: list[ToolInput]
    output_schema: Optional[AgentOutputSchemaInput]
    handoffs: list[HandoffInput]
    tracing: Required[ModelTracingInput]
    previous_response_id: Optional[str]


@activity.defn
@_auto_heartbeater
async def invoke_model_activity(input: ActivityModelInput) -> ModelResponse:
    """Activity that invokes a model with the given input."""
    # TODO: Is model caching needed here?
    model = MultiProvider().get_model(input.get("model_name"))
    activity.logger.info(model)

    async def empty_on_invoke_tool(ctx: RunContextWrapper[Any], input: str) -> str:
        return ""

    async def empty_on_invoke_handoff(ctx: RunContextWrapper[Any], input: str) -> Any:
        return None

    # workaround for https://github.com/pydantic/pydantic/issues/9541
    # ValidatorIterator returned
    input_json = json.dumps(input["input"], default=lambda o: str(o))
    input_input = json.loads(input_json)

    def make_tool(tool: ToolInput) -> Tool:
        if isinstance(tool, FileSearchTool):
            return cast(FileSearchTool, tool)
        elif isinstance(tool, WebSearchTool):
            return cast(WebSearchTool, tool)
        elif isinstance(tool, FunctionToolInput):
            t = cast(FunctionToolInput, tool)
            return FunctionTool(
                name=t.name,
                description=t.description,
                params_json_schema=t.params_json_schema,
                on_invoke_tool=empty_on_invoke_tool,
                strict_json_schema=t.strict_json_schema,
            )
        else:
            raise UserError(f"Unknown tool type: {tool.name}")

    tools = [make_tool(x) for x in input.get("tools", [])]
    handoffs = [
        Handoff(
            tool_name=x.tool_name,
            tool_description=x.tool_description,
            input_json_schema=x.input_json_schema,
            agent_name=x.agent_name,
            strict_json_schema=x.strict_json_schema,
            on_invoke_handoff=empty_on_invoke_handoff,
        )
        for x in input.get("handoffs", [])
    ]
    return await model.get_response(
        system_instructions=input.get("system_instructions"),
        input=input_input,
        model_settings=input["model_settings"],
        tools=tools,
        output_schema=input.get("output_schema"),
        handoffs=handoffs,
        tracing=ModelTracing(input["tracing"]),
        previous_response_id=input.get("previous_response_id"),
    )
