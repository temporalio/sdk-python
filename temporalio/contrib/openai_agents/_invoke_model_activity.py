"""A temporal activity that invokes a LLM model.

Implements mapping of OpenAI datastructures to Pydantic friendly types.
"""

import asyncio
import enum
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn, Union

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
from agents.items import TResponseStreamEvent
from openai import (
    APIStatusError,
    AsyncOpenAI,
)
from openai.types.responses import ResponseErrorEvent
from openai.types.responses.tool_param import Mcp
from pydantic_core import to_json
from typing_extensions import Required, TypedDict

from temporalio import activity, workflow
from temporalio.contrib.openai_agents._heartbeat_decorator import _auto_heartbeater
from temporalio.contrib.openai_agents._model_parameters import StreamingOptions
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


class ActivityModelInputWithSignal(ActivityModelInput):
    """Input for the stream_model activity."""

    signal: str


class ModelActivity:
    """Class wrapper for model invocation activities to allow model customization. By default, we use an OpenAIProvider with retries disabled.
    Disabling retries in your model of choice is recommended to allow activity retries to define the retry model.
    """

    def __init__(
        self, model_provider: ModelProvider | None, streaming_options: StreamingOptions
    ):
        """Initialize the activity with a model provider."""
        self._model_provider = model_provider or OpenAIProvider(
            openai_client=AsyncOpenAI(max_retries=0)
        )
        self._streaming_options = streaming_options

    @activity.defn
    @_auto_heartbeater
    async def invoke_model_activity(self, input: ActivityModelInput) -> ModelResponse:
        """Activity that invokes a model with the given input."""
        model = self._model_provider.get_model(input.get("model_name"))

        tools = _make_tools(input)
        handoffs = _make_handoffs(input)

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
            _handle_error(e)

    @activity.defn
    async def stream_model(self, input: ActivityModelInputWithSignal) -> None:
        """Activity that streams a model with the given input."""
        model = self._model_provider.get_model(input.get("model_name"))

        tools = _make_tools(input)
        handoffs = _make_handoffs(input)

        handle = activity.client().get_workflow_handle(
            workflow_id=activity.info().workflow_id
        )

        batch: list[TResponseStreamEvent] = []

        # If the activity previously failed, notify the stream
        if activity.info().attempt > 1:
            batch.append(
                ResponseErrorEvent(
                    message="Activity Failed",
                    sequence_number=0,
                    type="error",
                ))
        try:
            events = model.stream_response(
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

            async def send_batch():
                if batch:
                    await handle.signal(input["signal"], batch)
                    batch.clear()

            async def send_batches():
                while True:
                    await asyncio.sleep(self._streaming_options.signal_batch_latency_seconds)
                    await send_batch()

            async def read_events():
                async for event in events:
                    event.model_rebuild()
                    batch.append(event)
                    if self._streaming_options.callback is not None:
                        await self._streaming_options.callback(
                            input["model_settings"], event
                        )

            try:
                completed, pending = await asyncio.wait(
                    [
                        asyncio.create_task(read_events()),
                        asyncio.create_task(send_batches()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                for task in completed:
                    await task

            except StopAsyncIteration as e:
                pass
            # Send any remaining events in the batch
            if batch:
                await send_batch()

        except APIStatusError as e:
            _handle_error(e)

    @activity.defn
    async def batch_stream_model(
        self, input: ActivityModelInput
    ) -> list[TResponseStreamEvent]:
        """Activity that streams a model with the given input."""
        model = self._model_provider.get_model(input.get("model_name"))

        tools = _make_tools(input)
        handoffs = _make_handoffs(input)

        events = model.stream_response(
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
        result = []
        async for event in events:
            event.model_rebuild()
            result.append(event)
            if self._streaming_options.callback is not None:
                await self._streaming_options.callback(input["model_settings"], event)

        return result


async def _empty_on_invoke_tool(ctx: RunContextWrapper[Any], input: str) -> str:
    return ""


async def _empty_on_invoke_handoff(ctx: RunContextWrapper[Any], input: str) -> Any:
    return None


def _make_tool(tool: ToolInput) -> Tool:
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
            on_invoke_tool=_empty_on_invoke_tool,
            strict_json_schema=tool.strict_json_schema,
        )
    else:
        raise UserError(f"Unknown tool type: {tool.name}")


def _make_tools(input: ActivityModelInput) -> list[Tool]:
    return [_make_tool(x) for x in input.get("tools", [])]


def _make_handoffs(input: ActivityModelInput) -> list[Handoff[Any, Any]]:
    return [
        Handoff(
            tool_name=x.tool_name,
            tool_description=x.tool_description,
            input_json_schema=x.input_json_schema,
            agent_name=x.agent_name,
            strict_json_schema=x.strict_json_schema,
            on_invoke_handoff=_empty_on_invoke_handoff,
        )
        for x in input.get("handoffs", [])
    ]


def _handle_error(e: APIStatusError) -> NoReturn:
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
    if e.response.status_code in [408, 409, 429] or e.response.status_code >= 500:
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
