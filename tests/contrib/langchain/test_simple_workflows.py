from datetime import timedelta

import pytest

from temporalio.client import Client, WorkflowFailureError
from .simple_workflows import (
    ActivityAsToolTestWorkflow,
    ChainOfActivitiesInCodeWorkflow,
    LlmToolChainWorkflow,
    ToolAsActivityWorkflow,
)
from .simple_activities import (
    char_count_activity,
    greet_activity,
    simple_weather_check,
    simple_calculation,
    uppercase_activity,
    unique_word_count_activity,
    word_count_activity,
)
from tests.helpers import new_worker
from temporalio.contrib.langchain import get_wrapper_activities
from .simple_workflows import (
    InvokeModelWorkflow,
    UnwrappedInvokeModelWorkflow,
    ChainOfActivitiesWorkflow,
    ChainOfFunctionToolsWorkflow,
    ParallelChainOfActivitiesWorkflow,
)


@pytest.mark.asyncio
async def test_activity_as_tool_simple(temporal_client: Client, unique_workflow_id):
    """Test activity-as-tool conversion without LangChain imports."""
    async with new_worker(
        temporal_client,
        ActivityAsToolTestWorkflow,
        activities=[
            simple_weather_check,
            simple_calculation,
            *get_wrapper_activities(),
        ],
    ) as worker:
        result = await temporal_client.execute_workflow(
            ActivityAsToolTestWorkflow.run,
            "San Francisco",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # Verify tool conversion and execution
        assert result["weather_tool_name"] == "simple_weather_check"
        assert result["calc_tool_name"] == "simple_calculation"
        assert result["tools_tested"] == 2

        # Verify weather tool results (returned as dict from tool)
        weather_result = result["weather_result"]
        assert isinstance(weather_result, dict)
        assert weather_result["city"] == "San Francisco"
        assert weather_result["temperature"] == "25°C"
        assert weather_result["condition"] == "Sunny"

        # Verify calculation tool results (returned as dict from tool)
        calc_result = result["calc_result"]
        assert isinstance(calc_result, dict)
        assert calc_result["sum"] == 15.0
        assert calc_result["product"] == 50.0


@pytest.mark.asyncio
async def test_wrapped_llm_workflow_success(
    temporal_client: Client, unique_workflow_id
):
    """Wrapped LLM via model_as_activity should succeed and return dummy response."""
    async with new_worker(
        temporal_client,
        InvokeModelWorkflow,
        activities=get_wrapper_activities(),
    ) as worker:
        result = await temporal_client.execute_workflow(
            InvokeModelWorkflow.run,
            "hello",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert result == "dummy-response"


@pytest.mark.asyncio
async def test_unwrapped_llm_workflow_failure(
    temporal_client: Client, unique_workflow_id
):
    """Direct LLM invocation inside workflow should fail."""
    async with new_worker(
        temporal_client,
        UnwrappedInvokeModelWorkflow,
        activities=get_wrapper_activities(),
    ) as worker:
        with pytest.raises(WorkflowFailureError):
            await temporal_client.execute_workflow(
                UnwrappedInvokeModelWorkflow.run,
                "hello",
                id=f"{unique_workflow_id}-direct",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=2),
            )


@pytest.mark.parametrize(
    "workflow_class",
    [
        ChainOfFunctionToolsWorkflow,
        ChainOfActivitiesInCodeWorkflow,
        ChainOfActivitiesWorkflow,
    ],
)
@pytest.mark.asyncio
async def test_chain_of_activities_workflow(
    temporal_client: Client, unique_workflow_id, workflow_class
):
    """Test workflow that chains activities together. Do it both as regular code as a LangChain chain."""
    async with new_worker(
        temporal_client,
        workflow_class,
        activities=[uppercase_activity, greet_activity, *get_wrapper_activities()],
    ) as worker:
        result = await temporal_client.execute_workflow(
            workflow_class.run,
            "world",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        assert result == "Hello, WORLD!"


@pytest.mark.asyncio
async def test_parallel_chain_of_activities_workflow(
    temporal_client: Client, unique_workflow_id
):
    """Test workflow that chains activities together in parallel."""
    async with new_worker(
        temporal_client,
        ParallelChainOfActivitiesWorkflow,
        activities=[
            word_count_activity,
            char_count_activity,
            unique_word_count_activity,
            uppercase_activity,
            *get_wrapper_activities(),
        ],
    ) as worker:
        result = await temporal_client.execute_workflow(
            ParallelChainOfActivitiesWorkflow.run,
            "Hello, world! Hello, world!",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        assert (
            result
            == "Parallel Text Analysis Report:\n- Total Words: 4\n- Total Characters: 27\n- Unique Words: 2"
        )


@pytest.mark.asyncio
async def test_llm_tool_chain(temporal_client: Client, unique_workflow_id):
    """Wrapped LLM via model_as_activity should succeed and return dummy response."""
    async with new_worker(
        temporal_client,
        LlmToolChainWorkflow,
        activities=[uppercase_activity, *get_wrapper_activities()],
    ) as worker:
        result = await temporal_client.execute_workflow(
            LlmToolChainWorkflow.run,
            "hello",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert result == "DUMMY-RESPONSE"


@pytest.mark.asyncio
async def test_tool_as_activity(temporal_client: Client, unique_workflow_id):
    """Test tool as activity."""
    async with new_worker(
        temporal_client,
        ToolAsActivityWorkflow,
        activities=[*get_wrapper_activities()],
    ) as worker:
        result = await temporal_client.execute_workflow(
            ToolAsActivityWorkflow.run,
            "hello world",
            id=unique_workflow_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=20),
        )
        assert result == "Hello World"
