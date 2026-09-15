"""Extra modules that may have optional dependencies."""

from importlib import import_module
from types import ModuleType


def __getattr__(name: str) -> ModuleType:
    if name == "openai_agents":
        module_name = f"{__name__}.{name}"
        try:
            return import_module(module_name)
        except ModuleNotFoundError as err:
            if err.name != module_name:
                raise
            raise ImportError(
                "The OpenAI Agents integration has moved to the "
                "temporalio-openai-agents package. Install it with "
                "`uv add temporalio-openai-agents`."
            ) from err
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
