import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator, Optional, Union, no_type_check

import nexusrpc
import pytest
from agents import (
    Agent,
    AgentOutputSchemaBase,
    GuardrailFunctionOutput,
    Handoff,
    InputGuardrailTripwireTriggered,
    ItemHelpers,
    MessageOutputItem,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    Tool,
    TResponseInputItem,
    Usage,
    function_tool,
    handoff,
    input_guardrail,
    output_guardrail,
    trace,
)
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.items import (
    HandoffOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseStreamEvent,
)
from openai import APIStatusError, AsyncOpenAI, BaseModel
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_function_web_search import ActionSearch
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import ConfigDict, Field, TypeAdapter

import temporalio.api.cloud.namespace.v1
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy, SearchAttributeValueType
from temporalio.contrib import openai_agents
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    TestModel,
    TestModelProvider,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from tests.contrib.openai_agents.research_agents.research_manager import (
    ResearchManager,
)
from tests.helpers import new_worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name


class StaticTestModel(TestModel):
    __test__ = False
    responses: list[ModelResponse] = []

    def __init__(
        self,
    ) -> None:
        self._responses = iter(self.responses)
        super().__init__(lambda: next(self._responses))


class TestHelloModel(StaticTestModel):
    responses = [
        ModelResponse(
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
    ]


@workflow.defn
class HelloWorldAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You only respond in haikus.",
        )
        result = await Runner.run(starting_agent=agent, input=prompt)
        return result.final_output


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_hello_world_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(TestHelloModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(client, HelloWorldAgent) as worker:
        result = await client.execute_workflow(
            HelloWorldAgent.run,
            "Tell me about recursion in programming.",
            id=f"hello-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        if use_local_model:
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


@activity.defn
async def get_weather_country(city: str, country: str) -> Weather:
    """
    Get the weather for a given city in a country.
    """
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


@dataclass
class WeatherInput:
    city: str


@activity.defn
async def get_weather_object(input: WeatherInput) -> Weather:
    """
    Get the weather for a given city.
    """
    return Weather(
        city=input.city, temperature_range="14-20C", conditions="Sunny with wind."
    )


@activity.defn
async def get_weather_context(ctx: RunContextWrapper[str], city: str) -> Weather:
    """
    Get the weather for a given city.
    """
    return Weather(city=city, temperature_range="14-20C", conditions=ctx.context)


class ActivityWeatherService:
    @activity.defn
    async def get_weather_method(self, city: str) -> Weather:
        """
        Get the weather for a given city.
        """
        return Weather(
            city=city, temperature_range="14-20C", conditions="Sunny with wind."
        )


@nexusrpc.service
class WeatherService:
    get_weather_nexus_operation: nexusrpc.Operation[WeatherInput, Weather]


@nexusrpc.handler.service_handler(service=WeatherService)
class WeatherServiceHandler:
    @nexusrpc.handler.sync_operation
    async def get_weather_nexus_operation(
        self, ctx: nexusrpc.handler.StartOperationContext, input: WeatherInput
    ) -> Weather:
        return Weather(
            city=input.city, temperature_range="14-20C", conditions="Sunny with wind."
        )


class TestWeatherModel(StaticTestModel):
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
                ResponseFunctionToolCall(
                    arguments='{"input":{"city":"Tokyo"}}',
                    call_id="call",
                    name="get_weather_object",
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
                ResponseFunctionToolCall(
                    arguments='{"city":"Tokyo","country":"Japan"}',
                    call_id="call",
                    name="get_weather_country",
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
                ResponseFunctionToolCall(
                    arguments='{"city":"Tokyo"}',
                    call_id="call",
                    name="get_weather_context",
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
                ResponseFunctionToolCall(
                    arguments='{"city":"Tokyo"}',
                    call_id="call",
                    name="get_weather_method",
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


class TestNexusWeatherModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments='{"input":{"city":"Tokyo"}}',
                    call_id="call",
                    name="get_weather_nexus_operation",
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
                            text="Test nexus weather result",
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


@workflow.defn
class ToolsWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent[str](
            name="Tools Workflow",
            instructions="You are a helpful agent.",
            tools=[
                openai_agents.workflow.activity_as_tool(
                    get_weather, start_to_close_timeout=timedelta(seconds=10)
                ),
                openai_agents.workflow.activity_as_tool(
                    get_weather_object, start_to_close_timeout=timedelta(seconds=10)
                ),
                openai_agents.workflow.activity_as_tool(
                    get_weather_country, start_to_close_timeout=timedelta(seconds=10)
                ),
                openai_agents.workflow.activity_as_tool(
                    get_weather_context, start_to_close_timeout=timedelta(seconds=10)
                ),
                openai_agents.workflow.activity_as_tool(
                    ActivityWeatherService.get_weather_method,
                    start_to_close_timeout=timedelta(seconds=10),
                ),
            ],
        )
        result = await Runner.run(
            starting_agent=agent, input=question, context="Stormy"
        )
        return result.final_output


@workflow.defn
class NexusToolsWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent[str](
            name="Nexus Tools Workflow",
            instructions="You are a helpful agent.",
            tools=[
                openai_agents.workflow.nexus_operation_as_tool(
                    WeatherService.get_weather_nexus_operation,
                    service=WeatherService,
                    endpoint=make_nexus_endpoint_name(workflow.info().task_queue),
                    schedule_to_close_timeout=timedelta(seconds=10),
                ),
            ],
        )
        result = await Runner.run(
            starting_agent=agent, input=question, context="Stormy"
        )
        return result.final_output


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_tool_workflow(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(TestWeatherModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        ToolsWorkflow,
        activities=[
            get_weather,
            get_weather_object,
            get_weather_country,
            get_weather_context,
            ActivityWeatherService().get_weather_method,
        ],
    ) as worker:
        workflow_handle = await client.start_workflow(
            ToolsWorkflow.run,
            "What is the weather in Tokio?",
            id=f"tools-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert result == "Test weather result"

            events = []
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    events.append(e)

            assert len(events) == 11
            assert (
                "function_call"
                in events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Sunny with wind"
                in events[1]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "function_call"
                in events[2]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Sunny with wind"
                in events[3]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "function_call"
                in events[4]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Sunny with wind"
                in events[5]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "function_call"
                in events[6]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Stormy"
                in events[7]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "function_call"
                in events[8]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Sunny with wind"
                in events[9]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Test weather result"
                in events[10]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_nexus_tool_workflow(
    client: Client, env: WorkflowEnvironment, use_local_model: bool
):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(TestNexusWeatherModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        NexusToolsWorkflow,
        nexus_service_handlers=[WeatherServiceHandler()],
    ) as worker:
        await create_nexus_endpoint(worker.task_queue, client)

        workflow_handle = await client.start_workflow(
            NexusToolsWorkflow.run,
            "What is the weather in Tokio?",
            id=f"nexus-tools-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert result == "Test nexus weather result"

            events = []
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes") or e.HasField(
                    "nexus_operation_completed_event_attributes"
                ):
                    events.append(e)

            assert len(events) == 3
            assert (
                "function_call"
                in events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Sunny with wind"
                in events[
                    1
                ].nexus_operation_completed_event_attributes.result.data.decode()
            )
            assert (
                "Test nexus weather result"
                in events[2]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )


@no_type_check
class TestResearchModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='{"searches":[{"query":"best Caribbean surfing spots April","reason":"Identify locations with optimal surfing conditions in the Caribbean during April."},{"query":"top Caribbean islands for hiking April","reason":"Find Caribbean islands with excellent hiking opportunities that are ideal in April."},{"query":"Caribbean water sports destinations April","reason":"Locate Caribbean destinations offering a variety of water sports activities in April."},{"query":"surfing conditions Caribbean April","reason":"Understand the surfing conditions and which islands are suitable for surfing in April."},{"query":"Caribbean adventure travel hiking surfing","reason":"Explore adventure travel options that combine hiking and surfing in the Caribbean."},{"query":"best beaches for surfing Caribbean April","reason":"Identify which Caribbean beaches are renowned for surfing in April."},{"query":"Caribbean islands with national parks hiking","reason":"Find islands with national parks or reserves that offer hiking trails."},{"query":"Caribbean weather April surfing conditions","reason":"Research the weather conditions in April affecting surfing in the Caribbean."},{"query":"Caribbean water sports rentals April","reason":"Look for places where water sports equipment can be rented in the Caribbean during April."},{"query":"Caribbean multi-activity vacation packages","reason":"Look for vacation packages that offer a combination of surfing, hiking, and water sports."}]}',
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
        )
    ]
    for i in range(10):
        responses.append(
            ModelResponse(
                output=[
                    ResponseFunctionWebSearch(
                        id="",
                        status="completed",
                        type="web_search_call",
                        action=ActionSearch(query="", type="search"),
                    ),
                    ResponseOutputMessage(
                        id="",
                        content=[
                            ResponseOutputText(
                                text="Granada",
                                annotations=[],
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    ),
                ],
                usage=Usage(),
                response_id=None,
            )
        )
    responses.append(
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='{"follow_up_questions":[], "markdown_report":"report", "short_summary":"rep"}',
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
        )
    )


@workflow.defn
class ResearchWorkflow:
    @workflow.run
    async def run(self, query: str):
        return await ResearchManager().run(query)


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.timeout(120)
async def test_research_workflow(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(TestResearchModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        ResearchWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            ResearchWorkflow.run,
            "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
            id=f"research-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=120),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert result == "report"

            events = []
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    events.append(e)

            assert len(events) == 12
            assert (
                '"type":"output_text"'
                in events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            for i in range(1, 11):
                assert (
                    "web_search_call"
                    in events[i]
                    .activity_task_completed_event_attributes.result.payloads[0]
                    .data.decode()
                )

            assert (
                '"type":"output_text"'
                in events[11]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )


def orchestrator_agent() -> Agent:
    spanish_agent = Agent[None](
        name="spanish_agent",
        instructions="You translate the user's message to Spanish",
        handoff_description="An english to spanish translator",
    )

    french_agent = Agent[None](
        name="french_agent",
        instructions="You translate the user's message to French",
        handoff_description="An english to french translator",
    )

    italian_agent = Agent[None](
        name="italian_agent",
        instructions="You translate the user's message to Italian",
        handoff_description="An english to italian translator",
    )

    orchestrator_agent = Agent[None](
        name="orchestrator_agent",
        instructions=(
            "You are a translation agent. You use the tools given to you to translate."
            "If asked for multiple translations, you call the relevant tools in order."
            "You never translate on your own, you always use the provided tools."
        ),
        tools=[
            spanish_agent.as_tool(
                tool_name="translate_to_spanish",
                tool_description="Translate the user's message to Spanish",
            ),
            french_agent.as_tool(
                tool_name="translate_to_french",
                tool_description="Translate the user's message to French",
            ),
            italian_agent.as_tool(
                tool_name="translate_to_italian",
                tool_description="Translate the user's message to Italian",
            ),
        ],
    )
    return orchestrator_agent


def synthesizer_agent() -> Agent:
    return Agent(
        name="synthesizer_agent",
        instructions="You inspect translations, correct them if needed, and produce a final concatenated response.",
    )


@workflow.defn
class AgentsAsToolsWorkflow:
    @workflow.run
    async def run(self, msg: str) -> str:
        # Run the entire orchestration in a single trace
        with trace("Orchestrator evaluator"):
            orchestrator = orchestrator_agent()
            synthesizer = synthesizer_agent()

            orchestrator_result = await Runner.run(
                starting_agent=orchestrator, input=msg
            )

            for item in orchestrator_result.new_items:
                if isinstance(item, MessageOutputItem):
                    text = ItemHelpers.text_message_output(item)
                    if text:
                        print(f"  - Translation step: {text}")

            synthesizer_result = await Runner.run(
                starting_agent=synthesizer, input=orchestrator_result.to_input_list()
            )

        return synthesizer_result.final_output


class AgentAsToolsModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments='{"input":"I am full"}',
                    call_id="call",
                    name="translate_to_spanish",
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
                            text="Estoy lleno.",
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
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='The translation to Spanish is: "Estoy lleno."',
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
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='The translation to Spanish is: "Estoy lleno."',
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


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_agents_as_tools_workflow(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(AgentAsToolsModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        AgentsAsToolsWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            AgentsAsToolsWorkflow.run,
            "Translate to Spanish: 'I am full'",
            id=f"agents-as-tools-workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert result == 'The translation to Spanish is: "Estoy lleno."'

            events = []
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    events.append(e)

            assert len(events) == 4
            assert (
                "function_call"
                in events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Estoy lleno"
                in events[1]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "The translation to Spanish is:"
                in events[2]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "The translation to Spanish is:"
                in events[3]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )


class AirlineAgentContext(BaseModel):
    passenger_name: Optional[str] = None
    confirmation_number: Optional[str] = None
    seat_number: Optional[str] = None
    flight_number: Optional[str] = None


@function_tool(
    name_override="faq_lookup_tool",
    description_override="Lookup frequently asked questions.",
)
async def faq_lookup_tool(question: str) -> str:
    if "bag" in question or "baggage" in question:
        return (
            "You are allowed to bring one bag on the plane. "
            "It must be under 50 pounds and 22 inches x 14 inches x 9 inches."
        )
    elif "seats" in question or "plane" in question:
        return (
            "There are 120 seats on the plane. "
            "There are 22 business class seats and 98 economy seats. "
            "Exit rows are rows 4 and 16. "
            "Rows 5-8 are Economy Plus, with extra legroom. "
        )
    elif "wifi" in question:
        return "We have free wifi on the plane, join Airline-Wifi"
    return "I'm sorry, I don't know the answer to that question."


@function_tool
async def update_seat(
    context: RunContextWrapper[AirlineAgentContext],
    confirmation_number: str,
    new_seat: str,
) -> str:
    # Update the context based on the customer's input
    context.context.confirmation_number = confirmation_number
    context.context.seat_number = new_seat
    # Ensure that the flight number has been set by the incoming handoff
    assert context.context.flight_number is not None, "Flight number is required"
    return f"Updated seat to {new_seat} for confirmation number {confirmation_number}"


### HOOKS


async def on_seat_booking_handoff(
    context: RunContextWrapper[AirlineAgentContext],
) -> None:
    flight_number = f"FLT-{workflow.random().randint(100, 999)}"
    context.context.flight_number = flight_number


### AGENTS


def init_agents() -> Agent[AirlineAgentContext]:
    """
    Initialize the agents for the airline customer service workflow.
    :return: triage agent
    """
    faq_agent = Agent[AirlineAgentContext](
        name="FAQ Agent",
        handoff_description="A helpful agent that can answer questions about the airline.",
        instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
        You are an FAQ agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
        Use the following routine to support the customer.
        # Routine
        1. Identify the last question asked by the customer.
        2. Use the faq lookup tool to answer the question. Do not rely on your own knowledge.
        3. If you cannot answer the question, transfer back to the triage agent.""",
        tools=[faq_lookup_tool],
    )

    seat_booking_agent = Agent[AirlineAgentContext](
        name="Seat Booking Agent",
        handoff_description="A helpful agent that can update a seat on a flight.",
        instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
        You are a seat booking agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
        Use the following routine to support the customer.
        # Routine
        1. Ask for their confirmation number.
        2. Ask the customer what their desired seat number is.
        3. Use the update seat tool to update the seat on the flight.
        If the customer asks a question that is not related to the routine, transfer back to the triage agent. """,
        tools=[update_seat],
    )

    triage_agent = Agent[AirlineAgentContext](
        name="Triage Agent",
        handoff_description="A triage agent that can delegate a customer's request to the appropriate agent.",
        instructions=(
            f"{RECOMMENDED_PROMPT_PREFIX} "
            "You are a helpful triaging agent. You can use your tools to delegate questions to other appropriate agents."
        ),
        handoffs=[
            faq_agent,
            handoff(agent=seat_booking_agent, on_handoff=on_seat_booking_handoff),
        ],
    )

    faq_agent.handoffs.append(triage_agent)
    seat_booking_agent.handoffs.append(triage_agent)
    return triage_agent


class ProcessUserMessageInput(BaseModel):
    user_input: str
    chat_length: int


class CustomerServiceModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="Hi there! How can I assist you today?",
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
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments="{}",
                    call_id="call",
                    name="transfer_to_seat_booking_agent",
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
                            text="Could you please provide your confirmation number?",
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
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="Thanks! What seat number would you like to change to?",
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
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments='{"confirmation_number":"11111","new_seat":"window seat"}',
                    call_id="call",
                    name="update_seat",
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
                            text="Your seat has been updated to a window seat. If there's anything else you need, feel free to let me know!",
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


@workflow.defn
class CustomerServiceWorkflow:
    def __init__(self, input_items: list[TResponseInputItem] = []):
        self.chat_history: list[str] = []
        self.current_agent: Agent[AirlineAgentContext] = init_agents()
        self.context = AirlineAgentContext()
        self.input_items = input_items

    @workflow.run
    async def run(self, input_items: list[TResponseInputItem] = []):
        await workflow.wait_condition(
            lambda: workflow.info().is_continue_as_new_suggested()
            and workflow.all_handlers_finished()
        )
        workflow.continue_as_new(self.input_items)

    @workflow.query
    def get_chat_history(self) -> list[str]:
        return self.chat_history

    @workflow.update
    async def process_user_message(self, input: ProcessUserMessageInput) -> list[str]:
        length = len(self.chat_history)
        self.chat_history.append(f"User: {input.user_input}")
        with trace("Customer service", group_id=workflow.info().workflow_id):
            self.input_items.append({"content": input.user_input, "role": "user"})
            result = await Runner.run(
                starting_agent=self.current_agent,
                input=self.input_items,
                context=self.context,
            )

            for new_item in result.new_items:
                agent_name = new_item.agent.name
                if isinstance(new_item, MessageOutputItem):
                    self.chat_history.append(
                        f"{agent_name}: {ItemHelpers.text_message_output(new_item)}"
                    )
                elif isinstance(new_item, HandoffOutputItem):
                    self.chat_history.append(
                        f"Handed off from {new_item.source_agent.name} to {new_item.target_agent.name}"
                    )
                elif isinstance(new_item, ToolCallItem):
                    self.chat_history.append(f"{agent_name}: Calling a tool")
                elif isinstance(new_item, ToolCallOutputItem):
                    self.chat_history.append(
                        f"{agent_name}: Tool call output: {new_item.output}"
                    )
                else:
                    self.chat_history.append(
                        f"{agent_name}: Skipping item: {new_item.__class__.__name__}"
                    )
            self.input_items = result.to_input_list()
            self.current_agent = result.last_agent
        workflow.set_current_details("\n\n".join(self.chat_history))
        return self.chat_history[length:]

    @process_user_message.validator
    def validate_process_user_message(self, input: ProcessUserMessageInput) -> None:
        if not input.user_input:
            raise ValueError("User input cannot be empty.")
        if len(input.user_input) > 1000:
            raise ValueError("User input is too long. Please limit to 1000 characters.")
        if input.chat_length != len(self.chat_history):
            raise ValueError("Stale chat history. Please refresh the chat.")


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_customer_service_workflow(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(CustomerServiceModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    questions = ["Hello", "Book me a flight to PDX", "11111", "Any window seat"]

    async with new_worker(
        client,
        CustomerServiceWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            CustomerServiceWorkflow.run,
            id=f"customer-service-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        history: list[Any] = []
        for q in questions:
            message_input = ProcessUserMessageInput(
                user_input=q, chat_length=len(history)
            )
            new_history = await workflow_handle.execute_update(
                CustomerServiceWorkflow.process_user_message, message_input
            )
            history.extend(new_history)
            print(*new_history, sep="\n")

        await workflow_handle.cancel()

        with pytest.raises(WorkflowFailureError) as err:
            await workflow_handle.result()
        assert isinstance(err.value.cause, CancelledError)

        if use_local_model:
            events = []
            async for e in WorkflowHandle(
                client,
                workflow_handle.id,
                run_id=workflow_handle._first_execution_run_id,
            ).fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    events.append(e)

            assert len(events) == 6
            assert (
                "Hi there! How can I assist you today?"
                in events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "transfer_to_seat_booking_agent"
                in events[1]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Could you please provide your confirmation number?"
                in events[2]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Thanks! What seat number would you like to change to?"
                in events[3]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "update_seat"
                in events[4]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "Your seat has been updated to a window seat. If there's anything else you need, feel free to let me know!"
                in events[5]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )


class InputGuardrailModel(OpenAIResponsesModel):
    __test__ = False
    responses: list[ModelResponse] = [
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="The capital of California is Sacramento.",
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
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text="x=3",
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
    guardrail_responses = [
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='{"is_math_homework":false,"reasoning":"The question asked is about the capital of California, which is a geography-related query, not math."}',
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
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='{"is_math_homework":true,"reasoning":"The question involves solving an equation for a variable, which is a typical math homework problem."}',
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
        self._responses = iter(self.responses)
        self._guardrail_responses = iter(self.guardrail_responses)

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
        prompt: Union[ResponsePromptParam, None] = None,
    ) -> ModelResponse:
        if (
            system_instructions
            == "Check if the user is asking you to do their math homework."
        ):
            return next(self._guardrail_responses)
        else:
            return next(self._responses)


### 1. An agent-based guardrail that is triggered if the user is asking to do math homework
class MathHomeworkOutput(BaseModel):
    reasoning: str
    is_math_homework: bool
    model_config = ConfigDict(extra="forbid")


guardrail_agent: Agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
)


@input_guardrail
async def math_guardrail(
    context: RunContextWrapper[None],
    agent: Agent,
    input: Union[str, list[TResponseInputItem]],
) -> GuardrailFunctionOutput:
    """This is an input guardrail function, which happens to call an agent to check if the input
    is a math homework question.
    """
    result = await Runner.run(guardrail_agent, input, context=context.context)
    final_output = result.final_output_as(MathHomeworkOutput)

    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=final_output.is_math_homework,
    )


@workflow.defn
class InputGuardrailWorkflow:
    @workflow.run
    async def run(self, messages: list[str]) -> list[str]:
        agent = Agent(
            name="Customer support agent",
            instructions="You are a customer support agent. You help customers with their questions.",
            input_guardrails=[math_guardrail],
        )

        input_data: list[TResponseInputItem] = []
        results: list[str] = []

        for user_input in messages:
            input_data.append(
                {
                    "role": "user",
                    "content": user_input,
                }
            )

            try:
                result = await Runner.run(agent, input_data)
                results.append(result.final_output)
                # If the guardrail didn't trigger, we use the result as the input for the next run
                input_data = result.to_input_list()
            except InputGuardrailTripwireTriggered:
                # If the guardrail triggered, we instead add a refusal message to the input
                message = "Sorry, I can't help you with your math homework."
                results.append(message)
                input_data.append(
                    {
                        "role": "assistant",
                        "content": message,
                    }
                )
        return results


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_input_guardrail(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(
                InputGuardrailModel("", openai_client=AsyncOpenAI(api_key="Fake key"))
            )
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        InputGuardrailWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            InputGuardrailWorkflow.run,
            [
                "What's the capital of California?",
                "Can you help me solve for x: 2x + 5 = 11",
            ],
            id=f"input-guardrail-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert len(result) == 2
            assert result[0] == "The capital of California is Sacramento."
            assert result[1] == "Sorry, I can't help you with your math homework."


class OutputGuardrailModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="",
                    content=[
                        ResponseOutputText(
                            text='{"reasoning":"The phone number\'s area code (650) is associated with a region. However, the exact location is not definitive, but it\'s commonly linked to the San Francisco Peninsula in California, including cities like San Mateo, Palo Alto, and parts of Silicon Valley. It\'s important to note that area codes don\'t always guarantee a specific location due to mobile number portability.","response":"The area code 650 is typically associated with California, particularly the San Francisco Peninsula, including cities like Palo Alto and San Mateo.","user_name":null}',
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
        )
    ]


# The agent's output type
class MessageOutput(BaseModel):
    reasoning: str = Field(
        description="Thoughts on how to respond to the user's message"
    )
    response: str = Field(description="The response to the user's message")
    user_name: Optional[str] = Field(
        description="The name of the user who sent the message, if known"
    )
    model_config = ConfigDict(extra="forbid")


@output_guardrail
async def sensitive_data_check(
    context: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    phone_number_in_response = "650" in output.response
    phone_number_in_reasoning = "650" in output.reasoning

    return GuardrailFunctionOutput(
        output_info={
            "phone_number_in_response": phone_number_in_response,
            "phone_number_in_reasoning": phone_number_in_reasoning,
        },
        tripwire_triggered=phone_number_in_response or phone_number_in_reasoning,
    )


output_guardrail_agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    output_type=MessageOutput,
    output_guardrails=[sensitive_data_check],
)


@workflow.defn
class OutputGuardrailWorkflow:
    @workflow.run
    async def run(self) -> bool:
        try:
            await Runner.run(
                output_guardrail_agent,
                "My phone number is 650-123-4567. Where do you think I live?",
            )
            return True
        except OutputGuardrailTripwireTriggered:
            return False


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_output_guardrail(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(OutputGuardrailModel())
            if use_local_model
            else None,
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        OutputGuardrailWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            OutputGuardrailWorkflow.run,
            id=f"output-guardrail-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        result = await workflow_handle.result()

        if use_local_model:
            assert not result


class WorkflowToolModel(StaticTestModel):
    responses = [
        ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments="{}",
                    call_id="call",
                    name="run_tool",
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
                            text="",
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


@workflow.defn
class WorkflowToolWorkflow:
    @workflow.run
    async def run(self) -> None:
        agent: Agent = Agent(
            name="Assistant",
            instructions="You are a helpful assistant.",
            tools=[function_tool(self.run_tool)],
        )
        await Runner.run(
            agent,
            "My phone number is 650-123-4567. Where do you think I live?",
        )

    async def run_tool(self):
        print("Tool ran with self:", self)
        workflow.logger.info("Tool ran with self: %s", self)
        return None


async def test_workflow_method_tools(client: Client):
    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(WorkflowToolModel()),
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        WorkflowToolWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            WorkflowToolWorkflow.run,
            id=f"workflow-tool-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        await workflow_handle.result()


async def test_response_serialization():
    # This should not be used in another test, or this test needs to change to use another unloaded type
    from openai.types.responses.response_output_item import ImageGenerationCall

    data = json.loads(
        b'{"id": "msg_68757ec43348819d86709f0fcb70316301a1194a3e05b38c","type": "image_generation_call","status": "completed"}'
    )
    call = TypeAdapter(ImageGenerationCall).validate_python(data)
    model_response = ModelResponse(
        output=[
            call,
        ],
        usage=Usage(),
        response_id="",
    )
    encoded = await pydantic_data_converter.encode([model_response])


async def assert_status_retry_behavior(status: int, client: Client, should_retry: bool):
    def status_error(status: int):
        with workflow.unsafe.imports_passed_through():
            with workflow.unsafe.sandbox_unrestricted():
                import httpx
            raise APIStatusError(
                message="Something went wrong.",
                response=httpx.Response(
                    status_code=status, request=httpx.Request("GET", url="")
                ),
                body=None,
            )

    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                retry_policy=RetryPolicy(maximum_attempts=2),
            ),
            model_provider=TestModelProvider(TestModel(lambda: status_error(status))),
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        HelloWorldAgent,
    ) as worker:
        workflow_handle = await client.start_workflow(
            HelloWorldAgent.run,
            "Input",
            id=f"workflow-tool-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        with pytest.raises(WorkflowFailureError) as e:
            await workflow_handle.result()

        found = False
        async for event in workflow_handle.fetch_history_events():
            if event.HasField("activity_task_started_event_attributes"):
                found = True
                if should_retry:
                    assert event.activity_task_started_event_attributes.attempt == 2
                else:
                    assert event.activity_task_started_event_attributes.attempt == 1
        assert found


async def test_exception_handling(client: Client):
    await assert_status_retry_behavior(408, client, should_retry=True)
    await assert_status_retry_behavior(409, client, should_retry=True)
    await assert_status_retry_behavior(429, client, should_retry=True)
    await assert_status_retry_behavior(500, client, should_retry=True)

    await assert_status_retry_behavior(400, client, should_retry=False)
    await assert_status_retry_behavior(403, client, should_retry=False)
    await assert_status_retry_behavior(404, client, should_retry=False)


class CustomModelProvider(ModelProvider):
    def get_model(self, model_name: Optional[str]) -> Model:
        client = AsyncOpenAI(base_url="https://api.openai.com/v1")
        return OpenAIChatCompletionsModel(model="gpt-4o", openai_client=client)


async def test_chat_completions_model(client: Client):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=CustomModelProvider(),
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        WorkflowToolWorkflow,
    ) as worker:
        workflow_handle = await client.start_workflow(
            WorkflowToolWorkflow.run,
            id=f"workflow-tool-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        await workflow_handle.result()


class WaitModel(Model):
    async def get_response(
        self,
        system_instructions: Union[str, None],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Union[AgentOutputSchemaBase, None],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Union[str, None],
        prompt: Union[ResponsePromptParam, None] = None,
    ) -> ModelResponse:
        activity.logger.info("Waiting")
        await asyncio.sleep(1.0)
        activity.logger.info("Returning")
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

    def stream_response(
        self,
        system_instructions: Optional[str],
        input: Union[str, list[TResponseInputItem]],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: Optional[str],
        prompt: Optional[ResponsePromptParam],
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError()


async def test_heartbeat(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Relies on real timing, skip.")

    new_config = client.config()
    new_config["plugins"] = [
        openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                heartbeat_timeout=timedelta(seconds=0.5),
            ),
            model_provider=TestModelProvider(WaitModel()),
        )
    ]
    client = Client(**new_config)

    async with new_worker(
        client,
        HelloWorldAgent,
    ) as worker:
        workflow_handle = await client.start_workflow(
            HelloWorldAgent.run,
            "Tell me about recursion in programming.",
            id=f"workflow-tool-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5.0),
        )
        await workflow_handle.result()
