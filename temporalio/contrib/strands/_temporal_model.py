import json
from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any

from strands.models import Model
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


def _filter_serializable(state: dict[str, Any]) -> dict[str, Any]:
    """Keep invocation_state entries that JSON-serialize; drop the rest with a debug log."""
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in state.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            dropped.append(key)
            continue
        clean[key] = value
    if dropped:
        workflow.logger.debug(
            f"Dropping non-serializable invocation_state keys: {dropped}"
        )
    return clean


class TemporalModel(Model):
    """A Strands :class:`Model` that runs ``stream()`` as a Temporal activity.

    ``model_name`` selects which factory the plugin will invoke worker-side; it
    must match a key in ``StrandsPlugin(models={...})``. Construction of this
    :class:`TemporalModel` itself does no I/O, so it is safe to instantiate at
    module level.

    Pass this instance to ``Agent(model=...)`` inside the workflow; each call
    dispatches through the registered model activity with ``model_name`` in
    the input, and the worker resolves it against the plugin's factories.

    When ``streaming_topic`` is set, each ``StreamEvent`` is also published to
    the named topic on the workflow's
    :class:`temporalio.contrib.workflow_streams.WorkflowStream` for external
    consumers.
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
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
        """Configure the model name, activity options, and streaming settings."""
        self._model_name = model_name
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

    def update_config(self, **_model_config: Any) -> None:
        """No-op; the real model is configured worker-side via the plugin's factories."""
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
        clean_state = _filter_serializable(invocation_state) if invocation_state else {}
        if self._streaming_topic is not None:
            events = await workflow.execute_activity_method(
                ModelActivity.invoke_model_streaming,
                _StreamingInvokeModelInput(
                    model_name=self._model_name,
                    messages=messages,
                    invocation_state=clean_state,
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
                    model_name=self._model_name,
                    messages=messages,
                    invocation_state=clean_state,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                ),
                **self._options,
            )
        for event in events:
            yield event
