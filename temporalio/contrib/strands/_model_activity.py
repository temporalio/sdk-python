from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import timedelta

from strands.models import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient


@dataclass
class _InvokeModelInput:
    messages: Messages
    tool_specs: list[ToolSpec] | None = None
    system_prompt: str | None = None
    tool_choice: ToolChoice | None = None
    system_prompt_content: list[SystemContentBlock] | None = None


@dataclass
class _StreamingInvokeModelInput(_InvokeModelInput):
    streaming_topic: str = ""
    streaming_batch_interval_seconds: float = 0.1


class ModelActivity:
    """Holds the user-supplied model and exposes the model activities."""

    def __init__(self, model: Model) -> None:
        self._model = model

    @activity.defn(name="invoke_strands_model")
    async def invoke_model(self, input: _InvokeModelInput) -> list[StreamEvent]:
        return [event async for event in _stream(self._model, input)]

    @activity.defn(name="invoke_strands_model_streaming")
    async def invoke_model_streaming(
        self, input: _StreamingInvokeModelInput
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        stream = WorkflowStreamClient.from_within_activity(
            batch_interval=timedelta(seconds=input.streaming_batch_interval_seconds),
        )
        topic = stream.topic(input.streaming_topic)
        async with stream:
            async for event in _stream(self._model, input):
                activity.heartbeat()
                events.append(event)
                topic.publish(event)
        return events


def _stream(model: Model, input: _InvokeModelInput) -> AsyncIterable[StreamEvent]:
    return model.stream(
        input.messages,
        input.tool_specs,
        input.system_prompt,
        tool_choice=input.tool_choice,
        system_prompt_content=input.system_prompt_content,
    )
