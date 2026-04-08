"""A temporal activity that invokes a LLM model.

Implements mapping of OpenAI datastructures to Pydantic friendly types.
"""

import enum
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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
    Usage,
    UserError,
    WebSearchTool,
)
from openai import (
    APIStatusError,
    AsyncOpenAI,
)
from openai.types.responses import ResponseCompletedEvent
from openai.types.responses.tool_param import Mcp
from typing_extensions import Required, TypedDict

from temporalio import activity
from temporalio.contrib.openai_agents._heartbeat_decorator import _auto_heartbeater
from temporalio.contrib.pubsub import PubSubClient
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)

EVENTS_TOPIC = "events"


def _make_event(event_type: str, **data: object) -> bytes:
    return json.dumps(
        {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
    ).encode()


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


ToolInput = (
    FunctionToolInput
    | FileSearchTool
    | WebSearchTool
    | ImageGenerationTool
    | CodeInterpreterTool
    | HostedMCPToolInput
)


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

    model_name: str | None
    system_instructions: str | None
    input: Required[str | list[TResponseInputItem]]
    model_settings: Required[ModelSettings]
    tools: list[ToolInput]
    output_schema: AgentOutputSchemaInput | None
    handoffs: list[HandoffInput]
    tracing: Required[ModelTracingInput]
    previous_response_id: str | None
    conversation_id: str | None
    prompt: Any | None


class ModelActivity:
    """Class wrapper for model invocation activities to allow model customization. By default, we use an OpenAIProvider with retries disabled.
    Disabling retries in your model of choice is recommended to allow activity retries to define the retry model.
    """

    def __init__(self, model_provider: ModelProvider | None = None):
        """Initialize the activity with a model provider."""
        self._model_provider = model_provider or OpenAIProvider(
            openai_client=AsyncOpenAI(max_retries=0)
        )

    @activity.defn
    @_auto_heartbeater
    async def invoke_model_activity(self, input: ActivityModelInput) -> ModelResponse:
        """Activity that invokes a model with the given input."""
        model = self._model_provider.get_model(input.get("model_name"))

        async def empty_on_invoke_tool(
            _ctx: RunContextWrapper[Any], _input: str
        ) -> str:
            return ""

        async def empty_on_invoke_handoff(
            _ctx: RunContextWrapper[Any], _input: str
        ) -> Any:
            return None

        def make_tool(tool: ToolInput) -> Tool:
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
                raise UserError(f"Unknown tool type: {tool.name}")  # type:ignore[reportUnreachable]

        tools = [make_tool(x) for x in input.get("tools", [])]
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
            # Listen to server hints
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
            if (
                e.response.status_code in [408, 409, 429]
                or e.response.status_code >= 500
            ):
                raise ApplicationError(
                    f"Retryable OpenAI status code: {e.response.status_code}",
                    non_retryable=False,
                    next_retry_delay=retry_after,
                ) from e

            raise ApplicationError(
                f"Non retryable OpenAI status code: {e.response.status_code}",
                non_retryable=True,
                next_retry_delay=retry_after,
            ) from e

    @activity.defn
    @_auto_heartbeater
    async def invoke_model_activity_streaming(
        self, input: ActivityModelInput
    ) -> ModelResponse:
        """Streaming-aware model activity.

        Calls model.stream_response(), publishes token events via PubSubClient,
        and returns the complete ModelResponse constructed from the
        ResponseCompletedEvent at the end of the stream.
        """
        model = self._model_provider.get_model(input.get("model_name"))

        async def empty_on_invoke_tool(
            _ctx: RunContextWrapper[Any], _input: str
        ) -> str:
            return ""

        async def empty_on_invoke_handoff(
            _ctx: RunContextWrapper[Any], _input: str
        ) -> Any:
            return None

        def make_tool(tool: ToolInput) -> Tool:
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
                return HostedMCPTool(tool_config=tool.tool_config)
            elif isinstance(tool, FunctionToolInput):
                return FunctionTool(
                    name=tool.name,
                    description=tool.description,
                    params_json_schema=tool.params_json_schema,
                    on_invoke_tool=empty_on_invoke_tool,
                    strict_json_schema=tool.strict_json_schema,
                )
            else:
                raise UserError(f"Unknown tool type: {tool.name}")  # type:ignore[reportUnreachable]

        tools = [make_tool(x) for x in input.get("tools", [])]
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

        pubsub = PubSubClient.create(batch_interval=0.1)
        final_response = None
        text_buffer = ""
        thinking_buffer = ""
        thinking_active = False

        try:
            async with pubsub:
                pubsub.publish(
                    EVENTS_TOPIC, _make_event("LLM_CALL_START"), priority=True
                )

                async for event in model.stream_response(
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
                ):
                    activity.heartbeat()
                    etype = getattr(event, "type", None)

                    if etype == "response.output_text.delta":
                        text_buffer += event.delta
                        pubsub.publish(
                            EVENTS_TOPIC,
                            _make_event("TEXT_DELTA", delta=event.delta),
                        )
                    elif etype == "response.reasoning_summary_text.delta":
                        if not thinking_active:
                            thinking_active = True
                            pubsub.publish(
                                EVENTS_TOPIC, _make_event("THINKING_START")
                            )
                        thinking_buffer += event.delta
                        pubsub.publish(
                            EVENTS_TOPIC,
                            _make_event("THINKING_DELTA", delta=event.delta),
                        )
                    elif etype == "response.reasoning_summary_text.done":
                        if thinking_active:
                            pubsub.publish(
                                EVENTS_TOPIC,
                                _make_event(
                                    "THINKING_COMPLETE",
                                    content=thinking_buffer,
                                ),
                                priority=True,
                            )
                            thinking_buffer = ""
                            thinking_active = False
                    elif etype == "response.output_item.added":
                        item = event.item
                        if getattr(item, "type", None) == "function_call":
                            pubsub.publish(
                                EVENTS_TOPIC,
                                _make_event(
                                    "TOOL_CALL_START", tool_name=item.name
                                ),
                            )
                    elif isinstance(event, ResponseCompletedEvent):
                        final_response = event.response

                if text_buffer:
                    pubsub.publish(
                        EVENTS_TOPIC,
                        _make_event("TEXT_COMPLETE", text=text_buffer),
                        priority=True,
                    )
                pubsub.publish(
                    EVENTS_TOPIC,
                    _make_event("LLM_CALL_COMPLETE"),
                    priority=True,
                )

        except APIStatusError as e:
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

            if (
                e.response.status_code in [408, 409, 429]
                or e.response.status_code >= 500
            ):
                raise ApplicationError(
                    f"Retryable OpenAI status code: {e.response.status_code}",
                    non_retryable=False,
                    next_retry_delay=retry_after,
                ) from e

            raise ApplicationError(
                f"Non retryable OpenAI status code: {e.response.status_code}",
                non_retryable=True,
                next_retry_delay=retry_after,
            ) from e

        if final_response is None:
            raise ApplicationError(
                "Stream ended without ResponseCompletedEvent",
                non_retryable=True,
            )

        usage = Usage(
            requests=1,
            input_tokens=final_response.usage.input_tokens
            if final_response.usage
            else 0,
            output_tokens=final_response.usage.output_tokens
            if final_response.usage
            else 0,
        )
        return ModelResponse(
            output=final_response.output,
            usage=usage,
            response_id=final_response.id,
        )
