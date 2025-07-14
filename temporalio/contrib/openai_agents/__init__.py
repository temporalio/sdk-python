"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.
"""

import importlib
import inspect
import pkgutil

from pydantic import BaseModel

from temporalio.contrib.openai_agents._invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsTracingInterceptor,
)
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    TestModel,
    TestModelProvider,
    set_open_ai_agent_temporal_overrides,
)

from . import workflow

__all__ = [
    "ModelActivity",
    "ModelActivityParameters",
    "workflow",
    "set_open_ai_agent_temporal_overrides",
    "OpenAIAgentsTracingInterceptor",
    "TestModel",
    "TestModelProvider",
]


def _reload_models(module_name: str) -> None:
    """Recursively walk through modules and rebuild BaseModel classes."""
    module = importlib.import_module(module_name)

    # Process classes in the current module
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseModel) and obj is not BaseModel:
            obj.model_rebuild()

    # Recursively process submodules
    if hasattr(module, "__path__"):
        for _, submodule_name, _ in pkgutil.iter_modules(module.__path__):
            full_submodule_name = f"{module_name}.{submodule_name}"
            _reload_models(full_submodule_name)


# Recursively call model_rebuild() on all BaseModel classes in openai.types
_reload_models("openai.types")
