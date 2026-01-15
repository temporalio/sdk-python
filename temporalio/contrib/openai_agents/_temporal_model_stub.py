from __future__ import annotations

import asyncio
import logging

from temporalio import workflow
from temporalio.contrib.openai_agents._model_parameters import (
    ModelActivityParameters,
    StreamingOptions,
)

logger = logging.getLogger(__name__)

from collections.abc import AsyncIterator
from typing import Any

from agents import (
    Agent,
    AgentOutputSchema,
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    FileSearchTool,
    FunctionTool,
    Handoff,
    HostedMCPTool,
    ImageGenerationTool,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    WebSearchTool,
)
from agents.items import TResponseStreamEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam

from temporalio.contrib.openai_agents._invoke_model_activity import (
    ActivityModelInput,
    ActivityModelInputWithSignal,
    AgentOutputSchemaInput,
    FunctionToolInput,
    HandoffInput,
    HostedMCPToolInput,
    ModelActivity,
    ModelTracingInput,
    ToolInput,
)


class _TemporalModelStub(Model):  # type:ignore[reportUnusedClass]
    """A stub that allows invoking models as Temporal activities."""

    def __init__(
        self,
        model_name: str | None,
        *,
        model_params: ModelActivityParameters,
        agent: Agent[Any] | None,
        streaming_options: StreamingOptions,
    ) -> None:
        self.model_name = model_name
        self.model_params = model_params
        self.agent = agent
        self.stream_events: list[TResponseStreamEvent] = []
        self.streaming_options = streaming_options

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        tool_inputs = _make_tool_inputs(tools)
        handoff_inputs = _make_handoff_inputs(handoffs)
        output_schema_input = _make_output_schema_input(output_schema)

        activity_input = ActivityModelInput(
            model_name=self.model_name,
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tool_inputs,
            output_schema=output_schema_input,
            handoffs=handoff_inputs,
            tracing=ModelTracingInput(tracing.value),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

        summary = self._make_summary(system_instructions, input)

        if self.model_params.use_local_activity:
            return await workflow.execute_local_activity_method(
                ModelActivity.invoke_model_activity,
                activity_input,
                summary=summary,
                schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
                schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
                start_to_close_timeout=self.model_params.start_to_close_timeout,
                retry_policy=self.model_params.retry_policy,
                cancellation_type=self.model_params.cancellation_type,
            )
        else:
            return await workflow.execute_activity_method(
                ModelActivity.invoke_model_activity,
                activity_input,
                summary=summary,
                task_queue=self.model_params.task_queue,
                schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
                schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
                start_to_close_timeout=self.model_params.start_to_close_timeout,
                heartbeat_timeout=self.model_params.heartbeat_timeout,
                retry_policy=self.model_params.retry_policy,
                cancellation_type=self.model_params.cancellation_type,
                versioning_intent=self.model_params.versioning_intent,
                priority=self.model_params.priority,
            )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        if self.model_params.use_local_activity:
            raise ValueError("Streaming is not available with local activities.")

        tool_inputs = _make_tool_inputs(tools)
        handoff_inputs = _make_handoff_inputs(handoffs)
        output_schema_input = _make_output_schema_input(output_schema)

        summary = self._make_summary(system_instructions, input)

        stream_queue: asyncio.Queue[TResponseStreamEvent | None] = asyncio.Queue()

        async def handle_stream_event(events: list[TResponseStreamEvent]):
            for event in events:
                await stream_queue.put(event)

        signal_name = "model_stream_signal"
        workflow.set_signal_handler(signal_name, handle_stream_event)

        activity_input = ActivityModelInput(
            model_name=self.model_name,
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tool_inputs,
            output_schema=output_schema_input,
            handoffs=handoff_inputs,
            tracing=ModelTracingInput(tracing.value),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        if self.streaming_options.use_signals:
            handle = workflow.start_activity_method(
                ModelActivity.stream_model,
                args=[
                    ActivityModelInputWithSignal(**activity_input, signal=signal_name)
                ],
                summary=summary,
                task_queue=self.model_params.task_queue,
                schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
                schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
                start_to_close_timeout=self.model_params.start_to_close_timeout,
                heartbeat_timeout=self.model_params.heartbeat_timeout,
                retry_policy=self.model_params.retry_policy,
                cancellation_type=self.model_params.cancellation_type,
                versioning_intent=self.model_params.versioning_intent,
                priority=self.model_params.priority,
            )

            async def monitor_activity():
                try:
                    await handle
                finally:
                    await stream_queue.put(None)  # Signal end of stream

            monitor_task = asyncio.create_task(monitor_activity())

            async def generator() -> AsyncIterator[TResponseStreamEvent]:
                while True:
                    item = await stream_queue.get()
                    if item is None:
                        await monitor_task
                        return
                    yield item

            return generator()
        else:

            async def generator() -> AsyncIterator[TResponseStreamEvent]:
                results = await workflow.execute_activity_method(
                    ModelActivity.batch_stream_model,
                    args=[activity_input],
                    summary=summary,
                    task_queue=self.model_params.task_queue,
                    schedule_to_close_timeout=self.model_params.schedule_to_close_timeout,
                    schedule_to_start_timeout=self.model_params.schedule_to_start_timeout,
                    start_to_close_timeout=self.model_params.start_to_close_timeout,
                    heartbeat_timeout=self.model_params.heartbeat_timeout,
                    retry_policy=self.model_params.retry_policy,
                    cancellation_type=self.model_params.cancellation_type,
                    versioning_intent=self.model_params.versioning_intent,
                    priority=self.model_params.priority,
                )
                for event in results:
                    yield event

            return generator()

    def _make_summary(
        self, system_instructions: str | None, input: str | list[TResponseInputItem]
    ) -> str | None:
        if self.model_params.summary_override:
            return (
                self.model_params.summary_override
                if isinstance(self.model_params.summary_override, str)
                else (
                    self.model_params.summary_override.provide(
                        self.agent, system_instructions, input
                    )
                )
            )
        elif self.agent:
            return self.agent.name
        else:
            return None


def _make_tool_input(tool: Tool) -> ToolInput:
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
    elif isinstance(tool, HostedMCPTool):
        return HostedMCPToolInput(tool_config=tool.tool_config)
    elif isinstance(tool, FunctionTool):
        return FunctionToolInput(
            name=tool.name,
            description=tool.description,
            params_json_schema=tool.params_json_schema,
            strict_json_schema=tool.strict_json_schema,
        )
    else:
        raise ValueError(f"Unsupported tool type: {tool.name}")


def _make_tool_inputs(tools: list[Tool]) -> list[ToolInput]:
    return [_make_tool_input(x) for x in tools]


def _make_handoff_inputs(handoffs: list[Handoff]) -> list[HandoffInput]:
    return [
        HandoffInput(
            tool_name=x.tool_name,
            tool_description=x.tool_description,
            input_json_schema=x.input_json_schema,
            agent_name=x.agent_name,
            strict_json_schema=x.strict_json_schema,
        )
        for x in handoffs
    ]


def _make_output_schema_input(
    output_schema: AgentOutputSchemaBase | None,
) -> AgentOutputSchemaInput | None:
    if output_schema is not None and not isinstance(output_schema, AgentOutputSchema):
        raise TypeError(
            f"Only AgentOutputSchema is supported by Temporal Model, got {type(output_schema).__name__}"
        )

    agent_output_schema = output_schema
    return (
        None
        if agent_output_schema is None
        else AgentOutputSchemaInput(
            output_type_name=agent_output_schema.name(),
            is_wrapped=agent_output_schema._is_wrapped,
            output_schema=agent_output_schema.json_schema()
            if not agent_output_schema.is_plain_text()
            else None,
            strict_json_schema=agent_output_schema.is_strict_json_schema(),
        )
    )
