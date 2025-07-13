"""Support for using LangChain with Temporal workflows.

This module provides compatibility between LangChain and Temporal workflows,
allowing you to run LLM models and tools as Temporal activities.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

# Core wrapper functionality
from temporalio.contrib.langchain._simple_wrappers import (
    simple_model_as_activity as model_as_activity,
    simple_tool_as_activity as tool_as_activity,
    TemporalModelProxy as TemporalModelWrapper,
    TemporalToolProxy as TemporalToolWrapper,
    get_simple_wrapper_activities as get_wrapper_activities,
    ModelOutput,
    ModelCallInput,
    ToolCallInput,
    CallOutput,
)

# Advanced functionality
from temporalio.contrib.langchain._model_activity import ModelActivity
from temporalio.contrib.langchain._model_parameters import ModelActivityParameters
from temporalio.contrib.langchain.temporal_langchain import (
    workflow,
    ToolSerializationError,
)


# Tracing functionality (not yet implemented for current SDK version)
class TemporalLangChainTracingInterceptor:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "TemporalLangChainTracingInterceptor not yet implemented for this SDK version"
        )


def create_langchain_tracing_interceptor(*args, **kwargs):
    raise NotImplementedError(
        "create_langchain_tracing_interceptor not yet implemented for this SDK version"
    )


__all__ = [
    "ModelActivity",
    "ModelActivityParameters",
    "ModelOutput",
    "ModelCallInput",
    "ToolCallInput",
    "CallOutput",
    "workflow",
    "ToolSerializationError",
    "model_as_activity",
    "tool_as_activity",
    "TemporalModelWrapper",
    "TemporalToolWrapper",
    "get_wrapper_activities",
    "TemporalLangChainTracingInterceptor",
    "create_langchain_tracing_interceptor",
]
