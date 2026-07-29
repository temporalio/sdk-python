"""Workflow utilities for Google ADK agents integration with Temporal."""

import functools
import inspect
import typing
from typing import TYPE_CHECKING, Any, Callable

import temporalio.workflow
from temporalio import workflow

if TYPE_CHECKING:
    from google.adk.workflow import FunctionNode


def activity_tool(activity_def: Callable, **kwargs: Any) -> Callable:
    """Decorator/Wrapper to wrap a Temporal Activity as an ADK Tool.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    This ensures the activity's signature is preserved for ADK's tool schema generation
    while marking it as a tool that executes via 'workflow.execute_activity'.
    """

    @functools.wraps(activity_def)
    async def wrapper(*args: Any, **kw: Any):
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


def activity_node(
    activity_def: Callable,
    *,
    name: str | None = None,
    rerun_on_resume: bool = False,
    **kwargs: Any,
) -> "FunctionNode":
    """Wraps a Temporal Activity as a node for an ADK workflow graph.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    The returned :class:`~google.adk.workflow.FunctionNode` can be placed in a
    ``Workflow(edges=[...])`` graph or invoked from a dynamic node via
    ``ctx.run_node(...)``. The previous node's output (``node_input``) is
    passed to the activity: directly for a single-parameter activity, or bound
    by parameter name for a multi-parameter activity (``node_input`` must then
    be a dict). Outside a workflow (local ADK runs) the activity function is
    invoked directly.

    Args:
        activity_def: The ``@activity.defn`` function to run.
        name: Node name; defaults to the activity function's name.
        rerun_on_resume: Passed through to ``FunctionNode``. Keep the default
            ``False`` so that on a human-in-the-loop resume the node is
            fast-forwarded from the session instead of re-executing the
            activity.
        **kwargs: Activity execution options for
            ``workflow.execute_activity`` (e.g. ``start_to_close_timeout``).
            Prefer configuring retries here via ``retry_policy`` rather than
            wrapping the node with an ADK ``RetryConfig``, which would retry
            on top of Temporal's own activity retries.
    """
    from google.adk.workflow import FunctionNode

    sig = inspect.signature(activity_def)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    # Resolve hints against the activity's own module so that string
    # annotations (e.g. under `from __future__ import annotations`) still
    # resolve once copied onto the wrapper, whose globals differ.
    try:
        resolved_hints = typing.get_type_hints(activity_def)
    except Exception:
        resolved_hints = {}

    async def wrapper(node_input: Any = None) -> Any:
        options = kwargs.copy()

        if not params:
            activity_args: list[Any] = []
        elif len(params) == 1:
            activity_args = [node_input]
        else:
            if not isinstance(node_input, dict):
                raise TypeError(
                    f"Activity node '{activity_def.__name__}' takes"
                    f" {len(params)} parameters, so its input must be a dict"
                    f" of parameter names to values, got"
                    f" {type(node_input).__name__}."
                )
            bound = sig.bind(**node_input)
            bound.apply_defaults()
            activity_args = list(bound.arguments.values())

        if not temporalio.workflow.in_workflow():
            # Outside a workflow, like local adk runs, use the function directly.
            result = activity_def(*activity_args)
            if inspect.isawaitable(result):
                return await result
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

    # ADK's FunctionNode binds parameters from the wrapper's signature and
    # type hints: a single `node_input` parameter receives the previous
    # node's output directly. Set metadata explicitly rather than via
    # functools.wraps: copying the activity's multi-parameter signature (or a
    # `__wrapped__` link, which ADK's type-hint resolution follows) would make
    # ADK bind the activity's own parameters from workflow state instead.
    wrapper.__name__ = name or activity_def.__name__
    wrapper.__qualname__ = wrapper.__name__
    wrapper.__doc__ = activity_def.__doc__
    input_annotation = (
        resolved_hints.get(params[0].name, Any) if len(params) == 1 else Any
    )
    return_annotation = resolved_hints.get("return", Any)
    wrapper.__annotations__ = {
        "node_input": input_annotation,
        "return": return_annotation,
    }
    setattr(
        wrapper,
        "__signature__",
        inspect.Signature(
            parameters=[
                inspect.Parameter(
                    "node_input",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                    annotation=input_annotation,
                )
            ],
            return_annotation=return_annotation,
        ),
    )

    return FunctionNode(
        func=wrapper, name=wrapper.__name__, rerun_on_resume=rerun_on_resume
    )
