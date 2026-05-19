from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.types.streaming import StreamEvent

from temporalio import activity
from temporalio.contrib.common._heartbeat_decorator import auto_heartbeater
from temporalio.contrib.workflow_streams import WorkflowStreamClient


# Fields are typed as Any because strands TypedDicts (Message, ToolSpec) use
# NotRequired, which Python < 3.11's get_type_hints leaks through unchanged
# and the default JSON converter then fails to deserialize. Values flow
# through unchanged to ``Model.stream`` which accepts the raw dicts.
@dataclass
class _InvokeModelInput:
    model_name: str
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
    """Holds the registered model factories and exposes the model activities."""

    def __init__(self, factories: dict[str, Callable[[], Model]]) -> None:
        """Store the factories; models are constructed lazily on first use."""
        self._factories = factories
        self._models: dict[str, Model] = {}

    def _get_model(self, name: str) -> Model:
        if name not in self._models:
            if name not in self._factories:
                raise ValueError(
                    f"Unknown model name {name!r}. "
                    f"Known: {sorted(self._factories)}"
                )
            self._models[name] = self._factories[name]()
        return self._models[name]

    @activity.defn
    @auto_heartbeater
    async def invoke_model(self, input: _InvokeModelInput) -> list[StreamEvent]:
        """Run the named model and return its stream events as a list."""
        model = self._get_model(input.model_name)
        return [event async for event in _stream(model, input)]

    @activity.defn
    @auto_heartbeater
    async def invoke_model_streaming(
        self, input: _StreamingInvokeModelInput
    ) -> list[StreamEvent]:
        """Run the named model and publish each stream event to a WorkflowStream."""
        model = self._get_model(input.model_name)
        events: list[StreamEvent] = []
        stream = WorkflowStreamClient.from_within_activity(
            batch_interval=timedelta(seconds=input.streaming_batch_interval_seconds),
        )
        topic = stream.topic(input.streaming_topic)
        async with stream:
            async for event in _stream(model, input):
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
