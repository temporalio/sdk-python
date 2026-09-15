"""Extra modules that may have optional dependencies."""

from typing import NoReturn


def __getattr__(name: str) -> NoReturn:
    if name == "openai_agents":
        raise ImportError(
            "The OpenAI Agents integration has moved to the "
            "temporalio-openai-agents package. Install it with "
            "`uv add temporalio-openai-agents`."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
