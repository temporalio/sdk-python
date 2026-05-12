from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.workflow import ActivityCancellationType, VersioningIntent


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


class _ModelActivity:
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


class TemporalModel(Model):
    """Strands :class:`Model` whose ``stream()`` runs as a Temporal activity.

    Construct inside a workflow and pass to ``Agent(model=...)``. The concrete
    model is supplied worker-side via the ``model`` argument to
    :class:`StrandsPlugin`.

    When ``streaming_topic`` is set, each ``StreamEvent`` is also published to
    the named topic on the workflow's
    :class:`temporalio.contrib.workflow_streams.WorkflowStream` for external
    consumers (UIs, tracing). The workflow must host a ``WorkflowStream`` to
    receive the publishes; otherwise the signals are unhandled and dropped.
    """

    def __init__(
        self,
        *,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        activity_id: str | None = None,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        self._streaming_topic = streaming_topic
        self._streaming_batch_interval = streaming_batch_interval
        self._options: dict[str, Any] = {
            "task_queue": task_queue,
            "schedule_to_close_timeout": schedule_to_close_timeout,
            "schedule_to_start_timeout": schedule_to_start_timeout,
            "start_to_close_timeout": start_to_close_timeout,
            "heartbeat_timeout": heartbeat_timeout,
            "retry_policy": retry_policy,
            "cancellation_type": cancellation_type,
            "activity_id": activity_id,
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(
            "TemporalModel.structured_output is not supported. Use "
            "Agent(structured_output_model=...) which routes structured output "
            "through stream() via the structured_output_tool."
        )

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        if self._streaming_topic is not None:
            events = await workflow.execute_activity_method(
                _ModelActivity.invoke_model_streaming,
                _StreamingInvokeModelInput(
                    messages=messages,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                    streaming_topic=self._streaming_topic,
                    streaming_batch_interval_seconds=self._streaming_batch_interval.total_seconds(),
                ),
                **self._options,
            )
        else:
            events = await workflow.execute_activity_method(
                _ModelActivity.invoke_model,
                _InvokeModelInput(
                    messages=messages,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                ),
                **self._options,
            )
        for event in events:
            yield event
