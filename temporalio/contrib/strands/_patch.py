from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any

from strands.agent.agent import Agent
from strands.models import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from temporalio import workflow

from ._model import (
    _InvokeModelInput,
    _ModelActivity,
    _StreamingInvokeModelInput,
)

_original_agent_init = Agent.__init__
_options: dict[str, Any] = {}
_streaming_topic: str | None = None
_streaming_batch_interval: timedelta = timedelta(milliseconds=100)


class _ActivityDispatchModel(Model):
    """Stub installed by the patch; routes ``stream()`` through an activity."""

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(
            "Strands Agent.structured_output_async is not supported in workflow "
            "context. Use Agent(structured_output_model=...) which routes through "
            "stream() via the structured_output_tool."
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
        if _streaming_topic is not None:
            events = await workflow.execute_activity_method(
                _ModelActivity.invoke_model_streaming,
                _StreamingInvokeModelInput(
                    messages=messages,
                    tool_specs=tool_specs,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    system_prompt_content=system_prompt_content,
                    streaming_topic=_streaming_topic,
                    streaming_batch_interval_seconds=_streaming_batch_interval.total_seconds(),
                ),
                **_options,
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
                **_options,
            )
        for event in events:
            yield event


def _patched_agent_init(self: Agent, model: Any = None, *args: Any, **kwargs: Any) -> None:
    if workflow.in_workflow():
        if model is not None:
            raise ValueError(
                "Agent(model=...) must not be set inside a workflow. "
                "Pass the model to StrandsPlugin(model=...) instead so it "
                "runs as a Temporal activity."
            )
        model = _ActivityDispatchModel()
    _original_agent_init(self, model, *args, **kwargs)


def install_patch(
    options: dict[str, Any],
    streaming_topic: str | None,
    streaming_batch_interval: timedelta,
) -> None:
    global _options, _streaming_topic, _streaming_batch_interval
    _options = options
    _streaming_topic = streaming_topic
    _streaming_batch_interval = streaming_batch_interval
    setattr(Agent, "__init__", _patched_agent_init)


def uninstall_patch() -> None:
    setattr(Agent, "__init__", _original_agent_init)
