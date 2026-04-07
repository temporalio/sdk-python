"""Workflow utilities for Google ADK agents integration with Temporal."""

import inspect
from typing import Any, Callable

import temporalio.workflow
from temporalio import workflow


def activity_tool(activity_def: Callable, **kwargs: Any) -> Callable:
    """Decorator/Wrapper to wrap a Temporal Activity as an ADK Tool.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    This ensures the activity's signature is preserved for ADK's tool schema generation
    while marking it as a tool that executes via 'workflow.execute_activity'.
    """

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

    # Copy metadata
    wrapper.__name__ = activity_def.__name__
    wrapper.__doc__ = activity_def.__doc__
    setattr(wrapper, "__signature__", inspect.signature(activity_def))

    return wrapper
