# Tests of the mock objects
import pytest
from .mocks import SimpleMockModel, SimpleMockTool


def test_simple_mock_objects():
    """Test our simple mock objects work correctly."""
    model = SimpleMockModel("test response")
    tool = SimpleMockTool("test_tool", "test result")

    # Test model
    assert model.model_name == "simple-mock-model"
    assert "test response" in model.invoke("test input").content

    # Test tool
    assert tool.name == "test_tool"
    assert "test result" in tool.invoke("test input")


@pytest.mark.asyncio
async def test_async_mock_methods():
    """Test async methods on mock objects."""
    model = SimpleMockModel("async test")
    tool = SimpleMockTool("async_tool", "async result")

    # Test async model
    response = await model.ainvoke("async input")
    assert "async test" in response.content

    # Test async tool
    result = await tool.ainvoke("async input")
    assert "async result" in result
