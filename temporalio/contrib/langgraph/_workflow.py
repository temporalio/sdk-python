"""Workflow-side wrappers for executing LangGraph nodes inline in a workflow."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable
from inspect import iscoroutinefunction
from typing import Any, Callable

from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import CONFIG_KEY_RUNTIME

from temporalio import workflow
from temporalio.contrib.workflow_streams._stream import _PUBLISH_SIGNAL


def wrap_workflow(
    func: Callable[..., Any],
    *,
    streaming_topic: str | None = None,
    summary_fn: Callable[[tuple[Any, ...], dict[str, Any]], str | None] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a function as a workflow-side LangGraph node.

    Mirrors :func:`wrap_activity`: the outer wrapper resolves a stream
    writer and passes it to an inner ``run`` that invokes the user
    function with the writer installed. Workflow-side nodes publish
    synchronously to the in-workflow ``WorkflowStream`` (no signal
    round-trip); activity-side nodes go through ``WorkflowStreamClient``.

    Workflow-side nodes have no activity to carry a summary, so a
    truthy ``summary_fn`` result updates the workflow's current details
    via :func:`temporalio.workflow.set_current_details` (last-writer-wins).
    """

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if summary_fn is not None:
            summary = summary_fn(args, kwargs)
            if summary:
                workflow.set_current_details(summary)

        async def run(stream_writer: Callable[[Any], None] | None) -> Any:
            token = None
            if stream_writer is not None:
                config = var_child_runnable_config.get() or {}
                configurable = dict(config.get("configurable") or {})
                runtime = configurable.get(CONFIG_KEY_RUNTIME)
                if runtime is not None:
                    configurable[CONFIG_KEY_RUNTIME] = dataclasses.replace(
                        runtime, stream_writer=stream_writer
                    )
                    token = var_child_runnable_config.set(
                        {**config, "configurable": configurable}
                    )
            try:
                if iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                return func(*args, **kwargs)
            finally:
                if token is not None:
                    var_child_runnable_config.reset(token)

        if streaming_topic is None:
            return await run(stream_writer=None)
        publish_handler = workflow.get_signal_handler(_PUBLISH_SIGNAL)
        stream = getattr(publish_handler, "__self__")
        topic = stream.topic(streaming_topic)
        return await run(stream_writer=topic.publish)

    return wrapper
