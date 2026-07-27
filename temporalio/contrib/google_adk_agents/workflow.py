"""Workflow utilities for Google ADK agents integration with Temporal."""

import functools
import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, cast

import temporalio.workflow
from temporalio import workflow

_TOOL_CONTEXT_PARAM = "tool_context"


@dataclass(frozen=True)
class ToolContextSnapshot:
    """Serializable snapshot of the ADK ``ToolContext`` for activity-backed tools.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    ADK's ``ToolContext`` holds live, non-serializable objects, so it cannot
    cross the activity boundary: activity inputs are sent to the server and
    may run on a different worker than the workflow. This snapshot carries the
    serializable subset instead.

    Declare a parameter named ``tool_context`` annotated with this type on an
    activity wrapped by :func:`activity_tool`:

    .. code-block:: python

        @activity.defn
        async def get_weather(query: str, tool_context: ToolContextSnapshot) -> dict:
            db_url = tool_context.state.get("url", "")
            ...

    Exactly like a native ADK function tool's ``tool_context`` parameter, it
    is excluded from the LLM-facing tool schema and filled in at invocation
    time — here with a snapshot taken from the live ``ToolContext`` before the
    activity is scheduled.

    The snapshot is one-way: mutating it inside the activity does not
    propagate back to the session. To modify session state, return the needed
    information from the activity and apply it in workflow-side code (for
    example an ADK callback or a plain tool function).

    Attributes:
        state: The session state visible to this tool call, as a plain dict.
        function_call_id: The id of the function call being handled, when
            available.
    """

    state: dict[str, Any] = field(default_factory=dict)
    function_call_id: str | None = None


def _tool_context_parameter(activity_def: Callable) -> inspect.Parameter | None:
    """Returns the activity's ``tool_context`` parameter after validating it.

    ADK reserves the parameter name ``tool_context`` for context injection and
    never includes it in the LLM-facing tool schema, so a parameter with that
    name can only ever be filled by the integration. It must be annotated with
    :class:`ToolContextSnapshot` (or left unannotated).
    """
    parameter = inspect.signature(activity_def).parameters.get(_TOOL_CONTEXT_PARAM)
    if parameter is None:
        return None
    if parameter.annotation is inspect.Parameter.empty:
        return parameter
    try:
        resolved = typing.get_type_hints(activity_def).get(
            _TOOL_CONTEXT_PARAM, parameter.annotation
        )
    except Exception:
        resolved = parameter.annotation
    if resolved is ToolContextSnapshot:
        return parameter
    # Accept an optional annotation too (ToolContextSnapshot | None).
    if set(typing.get_args(resolved)) == {ToolContextSnapshot, type(None)}:
        return parameter
    annotation_name = getattr(resolved, "__name__", str(resolved))
    if getattr(resolved, "__module__", "").startswith("google.adk"):
        raise ValueError(
            f"Activity '{activity_def.__name__}' declares 'tool_context:"
            f" {annotation_name}', but ADK context objects are not serializable"
            " and cannot be activity arguments. Annotate the parameter with"
            " ToolContextSnapshot instead to receive the serializable subset"
            " (session state and function-call id)."
        )
    raise ValueError(
        f"Activity '{activity_def.__name__}' has a 'tool_context' parameter"
        f" annotated with {annotation_name}. The name 'tool_context' is"
        " reserved by ADK for context injection; annotate the parameter with"
        " ToolContextSnapshot to receive the serializable subset of the tool"
        " context."
    )


def _snapshot_tool_context(tool_context: Any) -> ToolContextSnapshot:
    """Builds the serializable snapshot from a live ADK ``ToolContext``."""
    state: dict[str, Any] = {}
    state_object = getattr(tool_context, "state", None)
    if state_object is not None:
        to_dict = getattr(state_object, "to_dict", None)
        state = dict(cast(Any, to_dict() if callable(to_dict) else state_object))
    return ToolContextSnapshot(
        state=state,
        function_call_id=getattr(tool_context, "function_call_id", None),
    )


def activity_tool(activity_def: Callable, **kwargs: Any) -> Callable:
    """Decorator/Wrapper to wrap a Temporal Activity as an ADK Tool.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    This ensures the activity's signature is preserved for ADK's tool schema generation
    while marking it as a tool that executes via 'workflow.execute_activity'.

    If the activity declares a parameter named ``tool_context``, it must be
    annotated with :class:`ToolContextSnapshot`. ADK excludes the parameter
    from the tool schema and injects the live ``ToolContext`` into the
    wrapper, which passes the activity a serializable snapshot of it (session
    state and function-call id) in that parameter's position.
    """
    tool_context_param = _tool_context_parameter(activity_def)

    @functools.wraps(activity_def)
    async def wrapper(*args: Any, **kw: Any):
        # ADK injects the live ToolContext by name; replace it with the
        # serializable snapshot the activity actually declares.
        if tool_context_param is not None and _TOOL_CONTEXT_PARAM in kw:
            kw[_TOOL_CONTEXT_PARAM] = _snapshot_tool_context(kw[_TOOL_CONTEXT_PARAM])

        # Inspect signature to bind arguments
        sig = inspect.signature(activity_def)
        bound = sig.bind(*args, **kw)
        bound.apply_defaults()

        # Convert to positional args for Temporal
        activity_args = list(bound.arguments.values())

        # Decorator kwargs are defaults.
        options = kwargs.copy()

        if not temporalio.workflow.in_workflow():
            # If executed outside a workflow, like when doing local adk runs, use the function directly
            result = activity_def(*args, **kw)
            if inspect.isawaitable(result):
                return await result
            else:
                return result

        if not activity_args:
            return await workflow.execute_activity(activity_def, **options)
        if len(activity_args) == 1:
            return await workflow.execute_activity(
                activity_def, activity_args[0], **options
            )
        return await workflow.execute_activity(
            activity_def, args=activity_args, **options
        )

    # functools.wraps copies name/doc/module/annotations/qualname/dict.
    # Signature must be set explicitly since the wrapper uses *args/**kw.
    setattr(wrapper, "__signature__", inspect.signature(activity_def))

    return wrapper
