"""Workflow utilities for Google Gemini SDK integration with Temporal.

This module provides utilities for using the Google Gemini SDK within Temporal
workflows.  The key entry points are:

- :func:`activity_as_tool` — converts a Temporal activity into a Gemini tool
  callable for use with automatic function calling (AFC).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._errors import GoogleGenAIError
from temporalio.workflow import ActivityConfig


def activity_as_tool(
    fn: Callable,
    *,
    activity_config: ActivityConfig | None = None,
) -> Callable:
    """Convert a Temporal activity into a Gemini-compatible async tool callable.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    Returns an async callable with the same name, docstring, and type signature as
    ``fn``. When Gemini's automatic function calling (AFC) invokes the returned
    callable from within a Temporal workflow, the call is executed as a Temporal
    activity via :func:`workflow.execute_activity`. Each tool invocation therefore
    appears as a separate, durable entry in the workflow event history.

    Because AFC is left **enabled**, the Gemini SDK owns the agentic loop — no
    manual ``while`` loop or ``run_agent()`` helper is required. Pass the returned
    callable directly to ``GenerateContentConfig(tools=[...])``.

    Args:
        fn: A Temporal activity function decorated with ``@activity.defn``.
        activity_config: Configuration for the activity execution (timeouts,
            retry policy, etc.).  Must set ``start_to_close_timeout`` or
            ``schedule_to_close_timeout`` — Temporal requires one, and there is
            no default; otherwise the tool call raises when the activity is
            invoked.

    Returns:
        An async callable suitable for use as a Gemini tool.

    Raises:
        GoogleGenAIError: If ``fn`` is not decorated with ``@activity.defn`` or
            has no activity name.
    """
    ret = activity._Definition.from_callable(fn)
    if not ret:
        raise GoogleGenAIError(
            "Bare function without @activity.defn decorator is not supported",
            "invalid_tool",
        )
    if ret.name is None:
        raise GoogleGenAIError(
            "Activity must have a name to be used as a Gemini tool",
            "invalid_tool",
        )

    config: ActivityConfig = {**(activity_config or {})}
    if "summary" not in config:
        config["summary"] = "tool_call"

    # For class-based activities the first parameter is 'self'.  Partially apply
    # it so that Gemini inspects only the user-facing parameters when building
    # the function-call schema, while the worker resolves the real instance at
    # execution time.
    params = list(inspect.signature(fn).parameters.keys())
    schema_fn: Callable = fn
    if params and params[0] == "self":
        partial = functools.partial(fn, None)
        setattr(partial, "__name__", fn.__name__)
        partial.__annotations__ = getattr(fn, "__annotations__", {})
        setattr(
            partial,
            "__temporal_activity_definition",
            getattr(fn, "__temporal_activity_definition", None),
        )
        partial.__doc__ = fn.__doc__
        schema_fn = partial

    activity_name: str = ret.name

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        sig = inspect.signature(schema_fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        activity_args = list(bound.arguments.values())
        return await temporal_workflow.execute_activity(
            activity_name,
            args=activity_args,
            **config,
        )

    wrapper.__name__ = schema_fn.__name__  # type: ignore
    wrapper.__doc__ = schema_fn.__doc__
    setattr(wrapper, "__signature__", inspect.signature(schema_fn))
    wrapper.__annotations__ = getattr(schema_fn, "__annotations__", {})

    return wrapper
