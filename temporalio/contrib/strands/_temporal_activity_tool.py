import inspect
import json
from collections.abc import Callable
from typing import Any

from strands.tools.decorator import FunctionToolMetadata
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

from temporalio import activity, workflow


class _TemporalActivityTool(AgentTool):
    """Strands ``AgentTool`` whose body dispatches a Temporal activity."""

    def __init__(self, activity_fn: Callable, options: dict[str, Any]) -> None:
        super().__init__()
        defn = activity._Definition.from_callable(activity_fn)
        if not defn or not defn.name:
            raise ValueError("activity_fn must be decorated with @activity.defn")
        self._activity_name = defn.name
        self._options = options
        self._signature = inspect.signature(activity_fn)
        spec = FunctionToolMetadata(activity_fn).extract_metadata()
        spec["name"] = self._activity_name
        self._spec: ToolSpec = spec

    @property
    def tool_name(self) -> str:
        return self._activity_name

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec

    @property
    def tool_type(self) -> str:
        return "temporal_activity"

    async def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        bound = self._signature.bind(**tool_use["input"])
        bound.apply_defaults()
        positional = list(bound.arguments.values())
        if not positional:
            result = await workflow.execute_activity(
                self._activity_name, **self._options
            )
        elif len(positional) == 1:
            result = await workflow.execute_activity(
                self._activity_name, positional[0], **self._options
            )
        else:
            result = await workflow.execute_activity(
                self._activity_name, args=positional, **self._options
            )
        yield ToolResultEvent(
            ToolResult(
                toolUseId=tool_use["toolUseId"],
                status="success",
                content=[{"text": _to_text(result)}],
            )
        )


def _to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return str(result)
