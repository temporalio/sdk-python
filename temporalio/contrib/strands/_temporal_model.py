from collections.abc import AsyncIterable, Callable
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.models.bedrock import BedrockModel
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from ._model_activity import (
    ModelActivity,
    _InvokeModelInput,
    _StreamingInvokeModelInput,
)


class TemporalModel(Model):
    """A Strands :class:`Model` that runs ``stream()`` as a Temporal activity.

    ``model_factory`` is called once on the worker (when the plugin is
    constructed) to produce the real model used inside the activity. Defaults
    to :class:`strands.models.bedrock.BedrockModel`, matching Strands' default.
    Construction of this :class:`TemporalModel` itself does no I/O, so it is
    safe to instantiate at module level — the lambda is just stored.

    Pass the same instance to ``StrandsPlugin(model=...)`` (so the plugin can
    register the model's activities) and to ``Agent(model=...)`` inside the
    workflow (so the agent dispatches through that activity).

    When ``streaming_topic`` is set, each ``StreamEvent`` is also published to
    the named topic on the workflow's
    :class:`temporalio.contrib.workflow_streams.WorkflowStream` for external
    consumers.
    """

    def __init__(
        self,
        *,
        model_factory: Callable[[], Model] = BedrockModel,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Configure the model factory, activity options, and streaming settings."""
        self._model_factory = model_factory
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
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }

    def _build_activity(self) -> ModelActivity:
        return ModelActivity(self._model_factory())

    def update_config(self, **_model_config: Any) -> None:
        """No-op; the real model is configured worker-side via ``model_factory``."""
        return None

    def get_config(self) -> dict[str, Any]:
        """Return an empty config; configuration lives on the worker-side model."""
        return {}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        """Not supported; use ``Agent(structured_output_model=...)`` instead."""
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
        """Run the model via the registered Temporal activity and yield events."""
        if self._streaming_topic is not None:
            events = await workflow.execute_activity_method(
                ModelActivity.invoke_model_streaming,
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
                ModelActivity.invoke_model,
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
