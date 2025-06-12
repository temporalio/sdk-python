import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Union

import pytest
from agents import (
    Agent,
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.models.multi_provider import MultiProvider
from openai import AsyncOpenAI, BaseModel
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents.invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from temporalio.contrib.openai_agents.temporal_tools import activity_as_tool
from tests.helpers import new_worker


class TestProvider(ModelProvider):
    __test__ = False

    def __init__(self, model: Model):
        self._model = model

    def get_model(self, model_name: str | None) -> Model:
        return self._model


class TestModel(OpenAIResponsesModel):
    __test__ = False

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
    ) -> None:
        super().__init__(model, openai_client)

    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Union[AgentOutputSchemaBase, None],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: Union[str, None],
    ) -> ModelResponse:
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="test", annotations=[], type="output_text"
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            usage=Usage(),
            response_id=None,
        )


@workflow.defn(sandboxed=False)
class HelloWorldAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent(
            name="Assistant",
            instructions="You only respond in haikus.",
        )  # type: Agent
        config = RunConfig(model="test_model")
        result = await Runner.run(agent, input=prompt, run_config=config)
        return result.final_output


async def test_hello_world_agent(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    set_open_ai_agent_temporal_overrides()

    model_activity = ModelActivity(
        TestProvider(
            TestModel(  # type: ignore
                "", openai_client=AsyncOpenAI(api_key="Fake key")
            )
        )
    )
    async with new_worker(
        client, HelloWorldAgent, activities=[model_activity.invoke_model_activity]
    ) as worker:
        result = await client.execute_workflow(
            HelloWorldAgent.run,
            "Tell me about recursion in programming.",
            id=f"hello-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        assert result == "test"


@dataclass
class Weather:
    city: str
    temperature_range: str
    conditions: str


@activity.defn
async def get_weather(city: str) -> Weather:
    """
    Get the weather for a given city.
    """
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


response_index: int = 0


class TestWeatherModel(OpenAIResponsesModel):
    __test__ = False

    responses = [
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments='{"city":"Tokyo"}',
                    call_id="call",
                    name="get_weather",
                    type="function_call",
                    id="id",
                    status="completed",
                )
            ],
            usage=Usage(),
            response_id=None,
        ),
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="Test weather result",
                            annotations=[],
                            type="output_text",
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            usage=Usage(),
            response_id=None,
        ),
    ]

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
    ) -> None:
        super().__init__(model, openai_client)

    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Union[AgentOutputSchemaBase, None],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: Union[str, None],
    ) -> ModelResponse:
        global response_index
        response = self.responses[response_index]
        response_index += 1
        return response


@workflow.defn(sandboxed=False)
class ToolsWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent(
            name="Hello world",
            instructions="You are a helpful agent.",
            tools=[activity_as_tool(get_weather)],
        )  # type: Agent
        config = RunConfig()
        result = await Runner.run(agent, input=question, run_config=config)
        return result.final_output


async def test_tool_workflow(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    set_open_ai_agent_temporal_overrides()
    model_activity = ModelActivity(
        TestProvider(
            TestWeatherModel(  # type: ignore
                "", openai_client=AsyncOpenAI(api_key="Fake key")
            )
        )
    )
    async with new_worker(
        client,
        ToolsWorkflow,
        activities=[model_activity.invoke_model_activity, get_weather],
    ) as worker:
        workflow_handle = await client.start_workflow(
            ToolsWorkflow.run,
            "What is the weather in Tokio?",
            id=f"tools-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        result = await workflow_handle.result()
        activity_count = 0
        async for e in workflow_handle.fetch_history_events():
            if e.HasField("activity_task_completed_event_attributes"):
                activity_count += 1

        assert activity_count == 3
        assert result == "Test weather result"
