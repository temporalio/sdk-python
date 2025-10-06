"""A temporal activity that invokes a LLM model.

Implements mapping of OpenAI datastructures to Pydantic friendly types.
"""

import enum
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn, Optional, Union

from agents import (
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    FileSearchTool,
    FunctionTool,
    Handoff,
    HostedMCPTool,
    ImageGenerationTool,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIProvider,
    RunContextWrapper,
    Tool,
    TResponseInputItem,
    UserError,
    WebSearchTool,
)
from openai import (
    APIStatusError,
    AsyncOpenAI,
)
from openai.types.responses.tool_param import Mcp
from typing_extensions import Required, TypedDict

from temporalio import activity
from temporalio.contrib.openai_agents._heartbeat_decorator import _auto_heartbeater
from temporalio.exceptions import ApplicationError


@dataclass
class HandoffInput:
    """Data conversion friendly representation of a Handoff. Contains only the fields which are needed by the model
    execution to determine what to handoff to, not the actual handoff invocation, which remains in the workflow context.
    """

    tool_name: str
    tool_description: str
    input_json_schema: dict[str, Any]
    agent_name: str
    strict_json_schema: bool = True


@dataclass
class FunctionToolInput:
    """Data conversion friendly representation of a FunctionTool. Contains only the fields which are needed by the model
    execution to determine what tool to call, not the actual tool invocation, which remains in the workflow context.
    """

    name: str
    description: str
    params_json_schema: dict[str, Any]
    strict_json_schema: bool = True


@dataclass
class HostedMCPToolInput:
    """Data conversion friendly representation of a HostedMCPTool. Contains only the fields which are needed by the model
    execution to determine what tool to call, not the actual tool invocation, which remains in the workflow context.
    """

    tool_config: Mcp


ToolInput = Union[
    FunctionToolInput,
    FileSearchTool,
    WebSearchTool,
    ImageGenerationTool,
    CodeInterpreterTool,
    HostedMCPToolInput,
]


@dataclass
class AgentOutputSchemaInput(AgentOutputSchemaBase):
    """Data conversion friendly representation of AgentOutputSchema."""

    output_type_name: Optional[str]
    is_wrapped: bool
    output_schema: Optional[dict[str, Any]]
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
    input: Required[Union[str, list[TResponseInputItem]]]
    model_settings: Required[ModelSettings]
    tools: list[ToolInput]
    output_schema: Optional[AgentOutputSchemaInput]
    handoffs: list[HandoffInput]
    tracing: Required[ModelTracingInput]
    previous_response_id: Optional[str]
    conversation_id: Optional[str]
    prompt: Optional[Any]


class ModelActivity:
    """Class wrapper for model invocation activities to allow model customization. By default, we use an OpenAIProvider with retries disabled.
    Disabling retries in your model of choice is recommended to allow activity retries to define the retry model.
    """

    def __init__(self, model_provider: Optional[ModelProvider] = None):
        """Initialize the activity with a model provider."""
        self._model_provider = model_provider or OpenAIProvider(
            openai_client=AsyncOpenAI(max_retries=0)
        )

    @staticmethod
    def _make_tool(tool: ToolInput) -> Tool:
        """Convert a ToolInput to a Tool."""

        async def empty_on_invoke_tool(ctx: RunContextWrapper[Any], input: str) -> str:
            return ""

        if isinstance(
            tool,
            (
                FileSearchTool,
                WebSearchTool,
                ImageGenerationTool,
                CodeInterpreterTool,
            ),
        ):
            return tool
        elif isinstance(tool, HostedMCPToolInput):
            return HostedMCPTool(
                tool_config=tool.tool_config,
            )
        elif isinstance(tool, FunctionToolInput):
            return FunctionTool(
                name=tool.name,
                description=tool.description,
                params_json_schema=tool.params_json_schema,
                on_invoke_tool=empty_on_invoke_tool,
                strict_json_schema=tool.strict_json_schema,
            )
        else:
            raise UserError(f"Unknown tool type: {tool.name}")

    @staticmethod
    def _prepare_tools_and_handoffs(
        input: ActivityModelInput,
    ) -> tuple[list[Tool], list[Handoff[Any, Any]]]:
        """Prepare tools and handoffs from activity input."""

        async def empty_on_invoke_handoff(
            ctx: RunContextWrapper[Any], input: str
        ) -> Any:
            return None

        tools = [ModelActivity._make_tool(x) for x in input.get("tools", [])]
        handoffs: list[Handoff[Any, Any]] = [
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
        return tools, handoffs

    @staticmethod
    def _handle_api_status_error(e: APIStatusError) -> NoReturn:
        """Handle APIStatusError with retry logic.

        This method always raises an exception and never returns normally.
        """
        retry_after = None
        retry_after_ms_header = e.response.headers.get("retry-after-ms")
        if retry_after_ms_header is not None:
            retry_after = timedelta(milliseconds=float(retry_after_ms_header))

        if retry_after is None:
            retry_after_header = e.response.headers.get("retry-after")
            if retry_after_header is not None:
                retry_after = timedelta(seconds=float(retry_after_header))

        should_retry_header = e.response.headers.get("x-should-retry")
        if should_retry_header == "true":
            raise e
        if should_retry_header == "false":
            raise ApplicationError(
                "Non retryable OpenAI error",
                non_retryable=True,
                next_retry_delay=retry_after,
            ) from e

        # Specifically retryable status codes
        if e.response.status_code in [408, 409, 429, 500]:
            raise ApplicationError(
                "Retryable OpenAI status code",
                non_retryable=False,
                next_retry_delay=retry_after,
            ) from e

        raise ApplicationError(
            "Non retryable OpenAI status code",
            non_retryable=True,
            next_retry_delay=retry_after,
        ) from e

    @activity.defn
    @_auto_heartbeater
    async def invoke_model_activity(self, input: ActivityModelInput) -> ModelResponse:
        """Activity that invokes a model with the given input."""
        model = self._model_provider.get_model(input.get("model_name"))
        tools, handoffs = self._prepare_tools_and_handoffs(input)

        try:
            return await model.get_response(
                system_instructions=input.get("system_instructions"),
                input=input["input"],
                model_settings=input["model_settings"],
                tools=tools,
                output_schema=input.get("output_schema"),
                handoffs=handoffs,
                tracing=ModelTracing(input["tracing"]),
                previous_response_id=input.get("previous_response_id"),
                conversation_id=input.get("conversation_id"),
                prompt=input.get("prompt"),
            )
        except APIStatusError as e:
            self._handle_api_status_error(e)

    @activity.defn
    @_auto_heartbeater
    async def invoke_model_streaming_activity(
        self, input: ActivityModelInput
    ) -> list[dict]:
        """Activity that invokes a model with streaming and collects all events."""
        model = self._model_provider.get_model(input.get("model_name"))
        tools, handoffs = self._prepare_tools_and_handoffs(input)

        try:
            # Get streaming response and collect all events
            stream = model.stream_response(
                system_instructions=input.get("system_instructions"),
                input=input["input"],
                model_settings=input["model_settings"],
                tools=tools,
                output_schema=input.get("output_schema"),
                handoffs=handoffs,
                tracing=ModelTracing(input["tracing"]),
                previous_response_id=input.get("previous_response_id"),
                conversation_id=input.get("conversation_id"),
                prompt=input.get("prompt"),
            )

            # Collect all streaming events and convert TResponseStreamEvent to serializable dict format
            collected_events: list[dict] = []
            async for event in stream:
                # Convert TResponseStreamEvent (Pydantic model) to JSON-serializable dict
                # Use mode='json' to ensure all nested objects are properly serialized
                event_data: Any
                if hasattr(event, "model_dump"):
                    event_data = event.model_dump(mode="json")
                else:
                    # Fallback for non-Pydantic objects
                    event_data = event

                stream_event_dict = {"type": "raw_response_event", "data": event_data}
                collected_events.append(stream_event_dict)

            return collected_events

        except APIStatusError as e:
            self._handle_api_status_error(e)
