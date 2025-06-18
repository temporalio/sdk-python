from __future__ import annotations

import logging

from temporalio import workflow

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from typing import Any, AsyncIterator, Optional, Sequence, Union, cast

    from agents import (
        AgentOutputSchema,
        AgentOutputSchemaBase,
        ComputerTool,
        FileSearchTool,
        FunctionTool,
        Handoff,
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

    from temporalio.contrib.openai_agents.invoke_model_activity import (
        ActivityModelInput,
        AgentOutputSchemaInput,
        FunctionToolInput,
        HandoffInput,
        ModelActivity,
        ModelTracingInput,
        ToolInput,
    )


class _TemporalModelStub(Model):
    """A stub that allows invoking models as Temporal activities."""

    def __init__(self, model_name: Optional[str], **kwargs) -> None:
        self.model_name = model_name
        self.kwargs = kwargs

    async def get_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Optional[str],
        prompt: Optional[ResponsePromptParam],
    ) -> ModelResponse:
        def get_summary(input: Union[str, list[TResponseInputItem]]) -> str:
            ### Activity summary shown in the UI
            try:
                max_size = 100
                if isinstance(input, str):
                    return input[:max_size]
                elif isinstance(input, list):
                    seq_input = cast(Sequence[Any], input)
                    last_item = seq_input[-1]
                    if isinstance(last_item, dict):
                        return last_item.get("content", "")[:max_size]
                    elif hasattr(last_item, "content"):
                        return str(getattr(last_item, "content"))[:max_size]
                    return str(last_item)[:max_size]
                elif isinstance(input, dict):
                    return input.get("content", "")[:max_size]
            except Exception as e:
                logger.error(f"Error getting summary: {e}")
            return ""

        def make_tool_info(tool: Tool) -> ToolInput:
            if isinstance(tool, FileSearchTool):
                return cast(FileSearchTool, tool)
            elif isinstance(tool, WebSearchTool):
                return cast(WebSearchTool, tool)
            elif isinstance(tool, ComputerTool):
                raise NotImplementedError(
                    "Computer search preview is not supported in Temporal model"
                )
            elif isinstance(tool, FunctionTool):
                t = cast(FunctionToolInput, tool)
                return FunctionToolInput(
                    name=t.name,
                    description=t.description,
                    params_json_schema=t.params_json_schema,
                    strict_json_schema=t.strict_json_schema,
                )
            else:
                raise ValueError(f"Unknown tool type: {tool.name}")

        tool_infos = [make_tool_info(x) for x in tools]
        handoff_infos = [
            HandoffInput(
                tool_name=x.tool_name,
                tool_description=x.tool_description,
                input_json_schema=x.input_json_schema,
                agent_name=x.agent_name,
                strict_json_schema=x.strict_json_schema,
            )
            for x in handoffs
        ]
        if output_schema is not None and not isinstance(
            output_schema, AgentOutputSchema
        ):
            raise TypeError(
                f"Only AgentOutputSchema is supported by Temporal Model, got {type(output_schema).__name__}"
            )
        agent_output_schema = output_schema
        output_schema_input = (
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

        activity_input = ActivityModelInput(
            model_name=self.model_name,
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tool_infos,
            output_schema=output_schema_input,
            handoffs=handoff_infos,
            tracing=ModelTracingInput(tracing.value),
            previous_response_id=previous_response_id,
            prompt=prompt,
        )
        return await workflow.execute_activity_method(
            ModelActivity.invoke_model_activity,
            activity_input,
            summary=get_summary(input),
            **self.kwargs,
        )

    def stream_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list][TResponseInputItem],  # type: ignore
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Optional[str],
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError("Temporal model doesn't support streams yet")
