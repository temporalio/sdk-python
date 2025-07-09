"""Schema edge case tests for LangChain integration."""

import pytest
from datetime import timedelta
from typing import Any, Dict, Optional, List
from dataclasses import dataclass
from pydantic import BaseModel

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.langchain import workflow as lc_workflow, get_wrapper_activities
from tests.helpers import new_worker


class UserModel(BaseModel):
    """Pydantic model for testing."""

    name: str
    age: int
    email: Optional[str] = None


@dataclass
class DataClassModel:
    """Dataclass for testing."""

    title: str
    count: int = 0


@activity.defn
async def activity_with_optional_params(
    required: str,
    optional: Optional[str] = None,
    default_value: int = 42,
    list_param: List[str] = None,
) -> Dict[str, Any]:
    """Activity with optional parameters and defaults."""
    if list_param is None:
        list_param = []
    return {
        "required": required,
        "optional": optional,
        "default_value": default_value,
        "list_param": list_param,
    }


@activity.defn
async def activity_with_pydantic_input(user: UserModel) -> Dict[str, Any]:
    """Activity that takes a Pydantic model as input."""
    return {
        "name": user.name,
        "age": user.age,
        "email": user.email,
        "model_type": "UserModel",
    }


@activity.defn
async def activity_with_pydantic_output(name: str, age: int) -> UserModel:
    """Activity that returns a Pydantic model."""
    return UserModel(name=name, age=age)


@activity.defn
async def activity_with_dataclass_input(data: DataClassModel) -> Dict[str, Any]:
    """Activity that takes a dataclass as input."""
    return {"title": data.title, "count": data.count, "model_type": "DataClassModel"}


@activity.defn
async def activity_with_reserved_word_params(
    class_: str,  # Reserved word with underscore
    from_: str,  # Another reserved word
    type_: str,  # Another reserved word
) -> Dict[str, Any]:
    """Activity with parameters that are Python reserved words."""
    return {"class": class_, "from": from_, "type": type_}


@activity.defn
async def activity_with_many_params(
    required: str,
    optional_str: str = "default",
    optional_int: int = 100,
    optional_bool: bool = True,
) -> Dict[str, Any]:
    """Activity with many parameters including optional ones."""
    return {
        "required": required,
        "optional_str": optional_str,
        "optional_int": optional_int,
        "optional_bool": optional_bool,
    }


@workflow.defn
class SchemaTestWorkflow:
    """Workflow for testing schema edge cases."""

    @workflow.run
    async def run(self, test_type: str) -> Dict[str, Any]:
        if test_type == "optional_params":
            tool = lc_workflow.activity_as_tool(
                activity_with_optional_params,
                start_to_close_timeout=timedelta(seconds=5),
            )
            return await tool["execute"](required="test")
        elif test_type == "pydantic_input":
            tool = lc_workflow.activity_as_tool(
                activity_with_pydantic_input,
                start_to_close_timeout=timedelta(seconds=5),
            )
            user = UserModel(name="John", age=30, email="john@example.com")
            return await tool["execute"](user=user)
        elif test_type == "pydantic_output":
            tool = lc_workflow.activity_as_tool(
                activity_with_pydantic_output,
                start_to_close_timeout=timedelta(seconds=5),
            )
            result = await tool["execute"](name="Jane", age=25)
            # Convert Pydantic model to dict for comparison
            return {"result": result.dict() if hasattr(result, "dict") else str(result)}
        elif test_type == "reserved_words":
            tool = lc_workflow.activity_as_tool(
                activity_with_reserved_word_params,
                start_to_close_timeout=timedelta(seconds=5),
            )
            return await tool["execute"](
                class_="MyClass", from_="source", type_="string"
            )
        else:
            return {"error": "unknown_test_type"}


def test_optional_params_schema():
    """Test schema generation for activities with optional parameters."""
    tool = lc_workflow.activity_as_tool(
        activity_with_optional_params, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_optional_params"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool

    # Schema should handle optional parameters
    # (exact format depends on implementation)


def test_pydantic_input_schema():
    """Test schema generation for activities with Pydantic model inputs."""
    tool = lc_workflow.activity_as_tool(
        activity_with_pydantic_input, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_pydantic_input"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool


def test_pydantic_output_schema():
    """Test schema generation for activities with Pydantic model outputs."""
    tool = lc_workflow.activity_as_tool(
        activity_with_pydantic_output, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_pydantic_output"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool


def test_reserved_word_params_schema():
    """Test schema generation for activities with reserved word parameters."""
    tool = lc_workflow.activity_as_tool(
        activity_with_reserved_word_params, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_reserved_word_params"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool


def test_many_params_schema():
    """Test schema generation for activities with many parameters."""
    tool = lc_workflow.activity_as_tool(
        activity_with_many_params, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_many_params"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool


def test_dataclass_input_schema():
    """Test schema generation for activities with dataclass inputs."""
    tool = lc_workflow.activity_as_tool(
        activity_with_dataclass_input, start_to_close_timeout=timedelta(seconds=5)
    )

    assert tool["name"] == "activity_with_dataclass_input"
    assert "execute" in tool
    assert callable(tool["execute"])
    assert "args_schema" in tool


@pytest.mark.asyncio
async def test_optional_params_execution(temporal_client: Client, unique_workflow_id):
    """Test execution of activity with optional parameters."""
    async with new_worker(
        temporal_client,
        SchemaTestWorkflow,
        activities=[
            *get_wrapper_activities(),
            activity_with_optional_params,
            activity_with_pydantic_input,
            activity_with_pydantic_output,
            activity_with_reserved_word_params,
            activity_with_dataclass_input,
            activity_with_many_params,
        ],
    ) as worker:
        result = await temporal_client.execute_workflow(
            SchemaTestWorkflow.run,
            "optional_params",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

        # Verify optional parameters work
        assert result["required"] == "test"
        assert result["optional"] is None
        assert result["default_value"] == 42
        assert result["list_param"] == []


@pytest.mark.asyncio
async def test_pydantic_model_execution(temporal_client: Client, unique_workflow_id):
    """Test execution of activity with Pydantic models."""
    async with new_worker(
        temporal_client,
        SchemaTestWorkflow,
        activities=[
            *get_wrapper_activities(),
            activity_with_optional_params,
            activity_with_pydantic_input,
            activity_with_pydantic_output,
            activity_with_reserved_word_params,
            activity_with_dataclass_input,
            activity_with_many_params,
        ],
    ) as worker:
        result = await temporal_client.execute_workflow(
            SchemaTestWorkflow.run,
            "pydantic_input",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

        # Verify Pydantic model was properly handled
        assert result["name"] == "John"
        assert result["age"] == 30
        assert result["email"] == "john@example.com"
        assert result["model_type"] == "UserModel"


@pytest.mark.asyncio
async def test_reserved_words_execution(temporal_client: Client, unique_workflow_id):
    """Test execution of activity with reserved word parameters."""
    async with new_worker(
        temporal_client,
        SchemaTestWorkflow,
        activities=[
            *get_wrapper_activities(),
            activity_with_optional_params,
            activity_with_pydantic_input,
            activity_with_pydantic_output,
            activity_with_reserved_word_params,
            activity_with_dataclass_input,
            activity_with_many_params,
        ],
    ) as worker:
        result = await temporal_client.execute_workflow(
            SchemaTestWorkflow.run,
            "reserved_words",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

        # Verify reserved word parameters work
        assert result["class"] == "MyClass"
        assert result["from"] == "source"
        assert result["type"] == "string"


def test_schema_validation_helpers():
    """Test helper functions for schema validation."""
    from pydantic import BaseModel

    # Test that we can distinguish Pydantic models
    assert issubclass(UserModel, BaseModel)

    # Test that dataclasses are handled differently
    assert hasattr(DataClassModel, "__dataclass_fields__")

    # Test that regular functions don't have these attributes
    def regular_function():
        pass

    assert not hasattr(regular_function, "__dataclass_fields__")
    assert not issubclass(type(regular_function), BaseModel)
