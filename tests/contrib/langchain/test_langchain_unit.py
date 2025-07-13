from datetime import timedelta

import pytest

from .simple_activities import simple_test_activity

from temporalio.contrib.langchain import workflow as lc_workflow
from temporalio.contrib.langchain import model_as_activity, tool_as_activity

from .simple_activities import simple_weather_check, simple_calculation


def test_activity_as_tool_schema():
    """Activity-as-tool conversion generates correct schema."""
    tool_spec = lc_workflow.activity_as_tool(
        simple_test_activity, start_to_close_timeout=timedelta(seconds=10)
    )

    # Verify tool specification structure
    assert "name" in tool_spec
    assert "description" in tool_spec
    assert "args_schema" in tool_spec
    assert "execute" in tool_spec

    assert tool_spec["name"] == "simple_test_activity"
    assert "Simple test activity" in tool_spec["description"]
    assert callable(tool_spec["execute"])


@pytest.mark.parametrize(
    "tool_name,expected_result",
    [
        ("weather_tool", "simple_weather_check"),
        ("calc_tool", "simple_calculation"),
    ],
)
def test_parameterized_activity_conversion(tool_name: str, expected_result: str):
    # TODO: confirm that this is the behavior that we want - right now the function name is the tool name and there is no way to override it.
    """Test parameterized activity-as-tool conversion."""
    tools = {"weather_tool": simple_weather_check, "calc_tool": simple_calculation}
    tool_spec = lc_workflow.activity_as_tool(
        tools[tool_name], start_to_close_timeout=timedelta(seconds=5)
    )

    # Tool name comes from activity function name
    assert tool_spec["name"] == expected_result
    assert callable(tool_spec["execute"])
    assert "args_schema" in tool_spec


def test_langchain_wrapper_creation():
    """Test wrapper creation and error handling."""

    # Test that the imports work
    assert callable(model_as_activity)
    assert callable(tool_as_activity)

    # Test invalid input handling
    with pytest.raises((ValueError, TypeError)):
        model_as_activity("not a model")

    with pytest.raises((ValueError, TypeError, AttributeError)):
        tool_as_activity("not a tool")

    # Test that wrappers reject None input
    with pytest.raises((ValueError, TypeError, AttributeError)):
        model_as_activity(None)

    with pytest.raises((ValueError, TypeError, AttributeError)):
        tool_as_activity(None)
