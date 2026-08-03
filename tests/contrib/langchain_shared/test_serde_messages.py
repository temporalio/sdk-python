"""Message and tool-schema serialization in the shared core."""

from __future__ import annotations

import pytest

from temporalio.contrib._langchain._messages import (
    dump_messages,
    dump_object,
    load_messages,
    load_object,
    tool_to_schema,
)

pytest.importorskip("langchain_core")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    ToolMessage,
)


def test_object_round_trip_preserves_subtype_and_tool_calls() -> None:
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "t", "args": {"x": 1}, "id": "call-1"}],
    )
    loaded = load_object(dump_object(msg))
    assert isinstance(loaded, AIMessage)
    assert loaded.tool_calls[0]["id"] == "call-1"
    assert loaded.tool_calls[0]["args"] == {"x": 1}


def test_messages_round_trip_preserves_order_and_types() -> None:
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
        ToolMessage(content="result", tool_call_id="c1"),
    ]
    loaded = load_messages(dump_messages(msgs))
    assert [type(m) for m in loaded] == [HumanMessage, AIMessage, ToolMessage]
    assert loaded[2].tool_call_id == "c1"


def test_tool_to_schema_carries_parameters() -> None:
    from langchain_core.tools import tool

    @tool
    def get_weather(city: str) -> str:
        """Return the weather for a city."""
        return city

    schema = tool_to_schema(get_weather)
    fn = schema["function"]
    assert fn["name"] == "get_weather"
    assert fn["description"]
    assert "city" in fn["parameters"]["properties"]
