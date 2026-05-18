from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.types.streaming import StreamEvent

from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient


# Fields are typed as Any because strands TypedDicts (Message, ToolSpec) use
# NotRequired, which Python < 3.11's get_type_hints leaks through unchanged
# and the default JSON converter then fails to deserialize. Values flow
# through unchanged to ``Model.stream`` which accepts the raw dicts.
@dataclass
class _InvokeModelInput:
    messages: Any
    tool_specs: Any = None
    system_prompt: str | None = None
    tool_choice: Any = None
    system_prompt_content: Any = None


@dataclass
class _StreamingInvokeModelInput(_InvokeModelInput):
    streaming_topic: str = ""
    streaming_batch_interval_seconds: float = 0.1


class ModelActivity:
    """Holds the user-supplied model and exposes the model activities."""

    def __init__(self, model: Model) -> None:
        """Store the model that activities will invoke."""
        self._model = model

    @activity.defn(name="invoke_strands_model")
    async def invoke_model(self, input: _InvokeModelInput) -> list[StreamEvent]:
        """Run the model and return its stream events as a list."""
        return [event async for event in _stream(self._model, input)]

    @activity.defn(name="invoke_strands_model_streaming")
    async def invoke_model_streaming(
        self, input: _StreamingInvokeModelInput
    ) -> list[StreamEvent]:
        """Run the model and publish each stream event to a WorkflowStream."""
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
