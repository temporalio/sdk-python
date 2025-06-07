from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from datetime import timedelta
    from typing import Any, AsyncIterator, Sequence, cast

    from agents import (
        AgentOutputSchema,
        AgentOutputSchemaBase,
        FileSearchTool,
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

    from temporalio.contrib.openai_agents.invoke_model_activity import (
        ActivityModelInput,
        AgentOutputSchemaInput,
        FunctionToolInput,
        HandoffInput,
        ModelTracingInput,
        ToolInput,
        invoke_model_activity,
    )


class _TemporalModelStub(Model):
    """A stub that allows invoking models as Temporal activities."""

    def __init__(self, model_name: str | None) -> None:
        self.model_name = model_name

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
    ) -> ModelResponse:
        def get_summary(input: str | list[TResponseInputItem]) -> str:
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
                print(f"Error getting summary: {e}")
            return ""

        def make_tool_info(tool: Tool) -> ToolInput:
            if tool.name == "file_search":
                return cast(FileSearchTool, tool)
            elif tool.name == "web_search_preview":
                return cast(WebSearchTool, tool)
            elif tool.name == "computer_search_preview":
                raise NotImplementedError(
                    "Computer search preview is not supported in Temporal model"
                )
            elif tool.name == "function_tool":
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
        agent_output_schema = cast(AgentOutputSchema, output_schema)
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
        )
        return await workflow.execute_activity(
            invoke_model_activity,
            activity_input,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=10),
            summary=get_summary(input),
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
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError("Temporal model doesn't support streams yet")
