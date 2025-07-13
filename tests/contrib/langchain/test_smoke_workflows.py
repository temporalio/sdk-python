from datetime import timedelta
import pytest
from temporalio.client import Client
from temporalio.contrib.langchain import get_wrapper_activities
from tests.contrib.langchain.smoke_activities import magic_function_activity
from .smoke_workflows import (
    ActivityAsToolOpenAIWorkflow,
    SimpleOpenAIWorkflow,
    ToolAsActivityOpenAIWorkflow,
)
from tests.helpers import new_worker
import os

# Skip all tests in this module if integration testing is not enabled
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY environment variable not set",
)


@pytest.mark.asyncio
async def test_simple_openai_workflow(temporal_client: Client, unique_workflow_id):
    """Test simple OpenAI workflow."""
    async with new_worker(
        temporal_client,
        SimpleOpenAIWorkflow,
        activities=[*get_wrapper_activities()],
    ) as worker:
        result = await temporal_client.execute_workflow(
            SimpleOpenAIWorkflow.run,
            "What is the capital of France?",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert "paris" in result["content"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_class", [ActivityAsToolOpenAIWorkflow, ToolAsActivityOpenAIWorkflow]
)
async def test_tool_openai_workflow(
    temporal_client: Client, unique_workflow_id, workflow_class
):
    """Test tool OpenAI workflow."""
    async with new_worker(
        temporal_client,
        workflow_class,
        activities=[magic_function_activity, *get_wrapper_activities()],
    ) as worker:
        result = await temporal_client.execute_workflow(
            workflow_class.run,
            "What is the value of magic_function(3)?",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert "5" in result["output"]
