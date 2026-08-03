"""LangChain object serialization shared by the LangChain-family plugins."""

from __future__ import annotations

from typing import Any


def dump_object(obj: Any) -> Any:
    """Serialize a single LangChain ``Serializable`` (message, tool call, …)."""
    from langchain_core.load import dumpd

    return dumpd(obj)


def load_object(data: Any) -> Any:
    """Rehydrate a value produced by :func:`dump_object`, preserving subtype."""
    from langchain_core.load import load

    return load(data)


def dump_messages(messages: Any) -> list[Any]:
    """Serialize a sequence of LangChain messages to their ``dumpd`` form."""
    from langchain_core.load import dumpd

    return [dumpd(m) for m in messages]


def load_messages(dumped: list[Any]) -> list[Any]:
    """Rehydrate messages serialized by :func:`dump_messages`."""
    from langchain_core.load import load

    return [load(d) for d in dumped]


def tool_to_schema(tool: Any) -> dict[str, Any]:
    """Advertise a tool to the model as a full OpenAI tool schema.

    Carries name + description + argument JSON schema so the model can build
    valid arguments, not just select the tool by name.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(tool)
