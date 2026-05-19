import inspect
import json
from collections.abc import Callable
from typing import Any

from strands.interrupt import Interrupt
from strands.tools.decorator import FunctionToolMetadata
from strands.types._events import ToolInterruptEvent, ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

from temporalio import activity, workflow
from temporalio.exceptions import ActivityError, ApplicationError

from ._failure_converter import STRANDS_INTERRUPT_TYPE


class TemporalActivityTool(AgentTool):
    """Strands ``AgentTool`` whose body dispatches a Temporal activity."""

    def __init__(self, activity_fn: Callable, options: dict[str, Any]) -> None:
        """Capture the target activity and the options to invoke it with."""
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
        """Name of the underlying Temporal activity."""
        return self._activity_name

    @property
    def tool_spec(self) -> ToolSpec:
        """Strands ToolSpec derived from the activity's signature."""
        return self._spec

    @property
    def tool_type(self) -> str:
        """Tool kind identifier used by Strands."""
        return "temporal_activity"

    async def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        """Execute the tool by dispatching to the bound Temporal activity."""
        bound = self._signature.bind(**tool_use["input"])
        bound.apply_defaults()
        positional = list(bound.arguments.values())
        try:
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
        except ActivityError as e:
            cause = e.__cause__
            if (
                isinstance(cause, ApplicationError)
                and cause.type == STRANDS_INTERRUPT_TYPE
            ):
                yield ToolInterruptEvent(tool_use, [Interrupt(**cause.details[0])])
                return
            raise
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
