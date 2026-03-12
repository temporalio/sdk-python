import asyncio
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    cast,
)

import nexusrpc
import pydantic
import pytest
from agents import (
    Agent,
    AgentBase,
    AgentOutputSchemaBase,
    CodeInterpreterTool,
    FileSearchTool,
    GuardrailFunctionOutput,
    Handoff,
    HostedMCPTool,
    ImageGenerationTool,
    InputGuardrailTripwireTriggered,
    ItemHelpers,
    MCPToolApprovalFunctionResult,
    MCPToolApprovalRequest,
    MessageOutputItem,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    Runner,
    SQLiteSession,
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
from agents.mcp import MCPServer, MCPServerStdio
from openai import APIStatusError, AsyncOpenAI, BaseModel
from openai.types.responses import (
    ResponseCodeInterpreterToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_file_search_tool_call import Result
from openai.types.responses.response_function_web_search import ActionSearch
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    McpApprovalRequest,
    McpCall,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import ConfigDict, Field, TypeAdapter

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.contrib import openai_agents
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
)
from temporalio.contrib.openai_agents._model_parameters import ModelSummaryProvider
from temporalio.contrib.openai_agents._openai_runner import _convert_agent
from temporalio.contrib.openai_agents._temporal_model_stub import (
    _TemporalModelStub,
)
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    ResponseBuilders,
    TestModel,
    TestModelProvider,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError, TemporalError
from temporalio.testing import WorkflowEnvironment
from temporalio.workflow import ActivityConfig
from tests.contrib.openai_agents.research_agents.research_manager import (
    ResearchManager,
)
from tests.helpers import assert_eventually, new_worker
from tests.helpers.nexus import make_nexus_endpoint_name


def hello_mock_model():
    return TestModel.returning_responses([ResponseBuilders.output_message("test")])


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

    model = hello_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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
async def get_weather_country(city: str, country: str) -> Weather:  # type: ignore[reportUnusedParameter]
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
        self,
        ctx: nexusrpc.handler.StartOperationContext,  # type: ignore[reportUnusedParameter]
        input: WeatherInput,  # type: ignore[reportUnusedParameter]
    ) -> Weather:
        return Weather(
            city=input.city, temperature_range="14-20C", conditions="Sunny with wind."
        )


def weather_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call('{"city":"Tokyo"}', "get_weather"),
            ResponseBuilders.tool_call(
                '{"input":{"city":"Tokyo"}}', "get_weather_object"
            ),
            ResponseBuilders.tool_call(
                '{"city":"Tokyo","country":"Japan"}', "get_weather_country"
            ),
            ResponseBuilders.tool_call('{"city":"Tokyo"}', "get_weather_context"),
            ResponseBuilders.tool_call('{"city":"Tokyo"}', "get_weather_method"),
            ResponseBuilders.output_message("Test weather result"),
        ]
    )


def nexus_weather_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call(
                '{"input":{"city":"Tokyo"}}', "get_weather_nexus_operation"
            ),
            ResponseBuilders.output_message("Test nexus weather result"),
        ]
    )


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
                openai_agents.workflow.activity_as_tool(
                    get_weather_failure,
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

    model = weather_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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


@activity.defn
async def get_weather_failure(city: str) -> Weather:  # type: ignore[reportUnusedParameter]
    """
    Get the weather for a given city.
    """
    raise ApplicationError("No weather", non_retryable=True)


def weather_failure_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call('{"city":"Tokyo"}', "get_weather_failure"),
        ]
    )


async def test_tool_failure_workflow(client: Client):
    async with AgentEnvironment(
        model=weather_failure_mock_model(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            ToolsWorkflow,
            activities=[
                get_weather_failure,
            ],
        ) as worker:
            workflow_handle = await client.start_workflow(
                ToolsWorkflow.run,
                "What is the weather in Tokio?",
                id=f"tools-failure-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=2),
            )
            with pytest.raises(WorkflowFailureError) as e:
                await workflow_handle.result()
            cause = e.value.cause
            assert isinstance(cause, ApplicationError)
            assert "Workflow failure exception in Agents Framework" in cause.message


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_nexus_tool_workflow(
    client: Client, env: WorkflowEnvironment, use_local_model: bool
):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    model = nexus_weather_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as agent_env:
        client = agent_env.applied_on_client(client)

        async with new_worker(
            client,
            NexusToolsWorkflow,
            nexus_service_handlers=[WeatherServiceHandler()],
        ) as worker:
            await env.create_nexus_endpoint(
                make_nexus_endpoint_name(worker.task_queue), worker.task_queue
            )

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
                    if e.HasField(
                        "activity_task_completed_event_attributes"
                    ) or e.HasField("nexus_operation_completed_event_attributes"):
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


def research_mock_model():
    responses = [
        ResponseBuilders.output_message(
            '{"searches":[{"query":"best Caribbean surfing spots April","reason":"Identify locations with optimal surfing conditions in the Caribbean during April."},{"query":"top Caribbean islands for hiking April","reason":"Find Caribbean islands with excellent hiking opportunities that are ideal in April."},{"query":"Caribbean water sports destinations April","reason":"Locate Caribbean destinations offering a variety of water sports activities in April."},{"query":"surfing conditions Caribbean April","reason":"Understand the surfing conditions and which islands are suitable for surfing in April."},{"query":"Caribbean adventure travel hiking surfing","reason":"Explore adventure travel options that combine hiking and surfing in the Caribbean."},{"query":"best beaches for surfing Caribbean April","reason":"Identify which Caribbean beaches are renowned for surfing in April."},{"query":"Caribbean islands with national parks hiking","reason":"Find islands with national parks or reserves that offer hiking trails."},{"query":"Caribbean weather April surfing conditions","reason":"Research the weather conditions in April affecting surfing in the Caribbean."},{"query":"Caribbean water sports rentals April","reason":"Look for places where water sports equipment can be rented in the Caribbean during April."},{"query":"Caribbean multi-activity vacation packages","reason":"Look for vacation packages that offer a combination of surfing, hiking, and water sports."}]}'
        )
    ]
    for _ in range(10):
        responses.append(
            ModelResponse(
                output=[
                    ResponseFunctionWebSearch(
                        id="",
                        status="completed",
                        type="web_search_call",
                        action=ActionSearch(query="", type="search"),
                    ),
                    ResponseBuilders.response_output_message("Granada"),
                ],
                usage=Usage(),
                response_id=None,
            )
        )
    responses.append(
        ResponseBuilders.output_message(
            '{"follow_up_questions":[], "markdown_report":"report", "short_summary":"rep"}'
        )
    )
    return TestModel.returning_responses(responses)


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

    model = research_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120),
            schedule_to_close_timeout=timedelta(seconds=120),
        ),
    ) as env:
        client = env.applied_on_client(client)

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


def agent_as_tools_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call('{"input":"I am full"}', "translate_to_spanish"),
            ResponseBuilders.output_message("Estoy lleno."),
            ResponseBuilders.output_message(
                'The translation to Spanish is: "Estoy lleno."'
            ),
            ResponseBuilders.output_message(
                'The translation to Spanish is: "Estoy lleno."'
            ),
        ]
    )


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_agents_as_tools_workflow(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    model = agent_as_tools_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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
    passenger_name: str | None = None
    confirmation_number: str | None = None
    seat_number: str | None = None
    flight_number: str | None = None


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
    try:
        flight_number = f"FLT-{workflow.random().randint(100, 999)}"
    except TemporalError:
        flight_number = "FLT-100"
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


def customer_service_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.output_message("Hi there! How can I assist you today?"),
            ResponseBuilders.tool_call("{}", "transfer_to_seat_booking_agent"),
            ResponseBuilders.output_message(
                "Could you please provide your confirmation number?"
            ),
            ResponseBuilders.output_message(
                "Thanks! What seat number would you like to change to?"
            ),
            ResponseBuilders.tool_call(
                '{"confirmation_number":"11111","new_seat":"window seat"}',
                "update_seat",
            ),
            ResponseBuilders.output_message(
                "Your seat has been updated to a window seat. If there's anything else you need, feel free to let me know!"
            ),
            ResponseBuilders.tool_call("{}", "transfer_to_triage_agent"),
            ResponseBuilders.output_message("You're welcome!"),
        ]
    )


@workflow.defn
class CustomerServiceWorkflow:
    def __init__(self, input_items: list[TResponseInputItem] = []):
        self.chat_history: list[str] = []
        self.current_agent: Agent[AirlineAgentContext] = init_agents()
        self.context = AirlineAgentContext()
        self.input_items = input_items

    @workflow.run
    async def run(self, _input_items: list[TResponseInputItem] = []):
        await workflow.wait_condition(lambda: False)
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

    model = customer_service_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

        questions = [
            "Hello",
            "Book me a flight to PDX",
            "11111",
            "Any window seat",
            "Take me back to the triage agent to say goodbye",
        ]

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

                assert len(events) == 8
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
                assert (
                    "transfer_to_triage_agent"
                    in events[6]
                    .activity_task_completed_event_attributes.result.payloads[0]
                    .data.decode()
                )
                assert (
                    "You're welcome!"
                    in events[7]
                    .activity_task_completed_event_attributes.result.payloads[0]
                    .data.decode()
                )


class InputGuardrailModel(OpenAIResponsesModel):
    __test__ = False
    responses: list[ModelResponse] = [
        ResponseBuilders.output_message("The capital of California is Sacramento."),
        ResponseBuilders.output_message("x=3"),
    ]
    guardrail_responses = [
        ResponseBuilders.output_message(
            '{"is_math_homework":false,"reasoning":"The question asked is about the capital of California, which is a geography-related query, not math."}'
        ),
        ResponseBuilders.output_message(
            '{"is_math_homework":true,"reasoning":"The question involves solving an equation for a variable, which is a typical math homework problem."}'
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
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
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
    _agent: Agent,
    input: str | list[TResponseInputItem],
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

    model = (
        InputGuardrailModel("", openai_client=AsyncOpenAI(api_key="Fake key"))
        if use_local_model
        else None
    )
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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


def output_guardrail_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.output_message(
                '{"reasoning":"The phone number\'s area code (650) is associated with a region. However, the exact location is not definitive, but it\'s commonly linked to the San Francisco Peninsula in California, including cities like San Mateo, Palo Alto, and parts of Silicon Valley. It\'s important to note that area codes don\'t always guarantee a specific location due to mobile number portability.","response":"The area code 650 is typically associated with California, particularly the San Francisco Peninsula, including cities like Palo Alto and San Mateo.","user_name":null}'
            )
        ]
    )


# The agent's output type
class MessageOutput(BaseModel):
    reasoning: str = Field(
        description="Thoughts on how to respond to the user's message"
    )
    response: str = Field(description="The response to the user's message")
    user_name: str | None = Field(
        description="The name of the user who sent the message, if known"
    )
    model_config = ConfigDict(extra="forbid")


@output_guardrail
async def sensitive_data_check(
    _context: RunContextWrapper, _agent: Agent, output: MessageOutput
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

    model = output_guardrail_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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


def workflow_tool_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call("{}", "run_tool"),
            ResponseBuilders.output_message("Workflow tool was used"),
        ]
    )


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
    async with AgentEnvironment(
        model=workflow_tool_mock_model(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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
    from openai.types.responses.response_output_item import LocalShellCall

    data = json.loads(
        b'{"id":"", "action":{"command": [],"env": {},"type": "exec"},"call_id":"","status":"completed","type":"local_shell_call"}'
    )
    call = TypeAdapter(LocalShellCall).validate_python(data)
    model_response = ModelResponse(
        output=[
            call,
        ],
        usage=Usage(),
        response_id="",
    )
    await pydantic_data_converter.encode([model_response])


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

    async with AgentEnvironment(
        model=TestModel(lambda: status_error(status)),
        model_params=ModelActivityParameters(
            retry_policy=RetryPolicy(maximum_attempts=2),
        ),
    ) as env:
        client = env.applied_on_client(client)

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
            with pytest.raises(WorkflowFailureError):
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
    def get_model(self, model_name: str | None) -> Model:
        client = AsyncOpenAI(base_url="https://api.openai.com/v1")
        return OpenAIChatCompletionsModel(model="gpt-4o", openai_client=client)


async def test_chat_completions_model(client: Client):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    async with AgentEnvironment(
        model_provider=CustomModelProvider(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

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
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs,  # type:ignore[reportMissingParameterType]
    ) -> ModelResponse:
        activity.logger.info("Waiting")
        await asyncio.sleep(1.0)
        activity.logger.info("Returning")
        return ResponseBuilders.output_message("test")

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs,  # type:ignore[reportMissingParameterType]
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError()


@workflow.defn
class AlternateModelAgent:
    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You only respond in haikus.",
            model="test_model",
        )
        result = await Runner.run(starting_agent=agent, input=prompt)
        return result.final_output


class CheckModelNameProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        assert model_name == "test_model"
        return hello_mock_model()


async def test_alternative_model(client: Client):
    async with AgentEnvironment(
        model_provider=CheckModelNameProvider(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            AlternateModelAgent,
        ) as worker:
            workflow_handle = await client.start_workflow(
                AlternateModelAgent.run,
                "Hello",
                id=f"alternative-model-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            await workflow_handle.result()


async def test_heartbeat(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Relies on real timing, skip.")

    async with AgentEnvironment(
        model=WaitModel(),
        model_params=ModelActivityParameters(
            heartbeat_timeout=timedelta(seconds=0.5),
        ),
    ) as agent_env:
        client = agent_env.applied_on_client(client)

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


@workflow.defn
class SessionWorkflow:
    @workflow.run
    async def run(self) -> None:
        agent: Agent = Agent(
            name="Assistant",
            instructions="You are a helpful assistant.",
        )
        await Runner.run(
            agent,
            "My phone number is 650-123-4567. Where do you think I live?",
            session=SQLiteSession(session_id="id"),
        )


async def test_session(client: Client):
    async with AgentEnvironment(model=hello_mock_model()) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            SessionWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                SessionWorkflow.run,
                id=f"session-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10.0),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

            async def check():
                async for evt in workflow_handle.fetch_history_events():
                    # Sometimes just creating the sqlite session takes too long for a workflow in CI, so check both
                    if evt.HasField("workflow_task_failed_event_attributes") and (
                        "Temporal workflows don't support SQLite sessions"
                        in evt.workflow_task_failed_event_attributes.failure.message
                        or "Potential deadlock detected"
                        in evt.workflow_task_failed_event_attributes.failure.message
                    ):
                        return

            await assert_eventually(check)


async def test_lite_llm(client: Client):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")
    if sys.version_info >= (3, 14):
        pytest.skip("Lite LLM does not yet support Python 3.14")  # type:ignore[reportUnreachable]

    from agents.extensions.models.litellm_provider import (
        LitellmProvider,  # type:ignore[reportUnreachable]
    )

    async with AgentEnvironment(
        model_provider=LitellmProvider(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as agent_env:
        client = agent_env.applied_on_client(client)  # type:ignore[reportUnreachable]

        async with new_worker(
            client,
            HelloWorldAgent,
        ) as worker:
            workflow_handle = await client.start_workflow(  # type:ignore[reportUnreachable]
                HelloWorldAgent.run,
                "Tell me about recursion in programming",
                id=f"lite-llm-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            await workflow_handle.result()


def file_search_tool_mock_model():
    return TestModel.returning_responses(
        [
            ModelResponse(
                output=[
                    ResponseFileSearchToolCall(
                        queries=["side character in the Iliad"],
                        type="file_search_call",
                        id="id",
                        status="completed",
                        results=[
                            Result(text="Some scene"),
                            Result(text="Other scene"),
                        ],
                    ),
                    ResponseBuilders.response_output_message("Patroclus"),
                ],
                usage=Usage(),
                response_id=None,
            ),
        ]
    )


@workflow.defn
class FileSearchToolWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent[str](
            name="File Search Workflow",
            instructions="You are a librarian. You should use your tools to source all your information.",
            tools=[
                FileSearchTool(
                    max_num_results=3,
                    vector_store_ids=["vs_687fd7f5e69c8191a2740f06bc9a159d"],
                    include_search_results=True,
                )
            ],
        )
        result = await Runner.run(starting_agent=agent, input=question)

        # A file search was performed
        assert any(
            isinstance(item, ToolCallItem)
            and isinstance(item.raw_item, ResponseFileSearchToolCall)
            for item in result.new_items
        )
        return result.final_output


@pytest.mark.parametrize("use_local_model", [True, False])
async def test_file_search_tool(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    model = file_search_tool_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30)
        ),
        model=model,
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            FileSearchToolWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                FileSearchToolWorkflow.run,
                "Tell me about a side character in the Iliad.",
                id=f"file-search-tool-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await workflow_handle.result()
            if use_local_model:
                assert result == "Patroclus"


def image_generation_mock_model():
    return TestModel.returning_responses(
        [
            ModelResponse(
                output=[
                    ImageGenerationCall(
                        type="image_generation_call",
                        id="id",
                        status="completed",
                    ),
                    ResponseBuilders.response_output_message("Patroclus"),
                ],
                usage=Usage(),
                response_id=None,
            ),
        ]
    )


@workflow.defn
class ImageGenerationWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent[str](
            name="Image Generation Workflow",
            instructions="You are a helpful agent.",
            tools=[
                ImageGenerationTool(
                    tool_config={"type": "image_generation", "quality": "low"},
                )
            ],
        )
        result = await Runner.run(starting_agent=agent, input=question)

        # An image generation was performed
        assert any(
            isinstance(item, ToolCallItem)
            and isinstance(item.raw_item, ImageGenerationCall)
            for item in result.new_items
        )
        return result.final_output


# Can't currently validate against real server, we aren't verified for image generation
@pytest.mark.parametrize("use_local_model", [True])
async def test_image_generation_tool(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    model = image_generation_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30)
        ),
        model=model,
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            ImageGenerationWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                ImageGenerationWorkflow.run,
                "Create an image of a frog eating a pizza, comic book style.",
                id=f"image-generation-tool-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            await workflow_handle.result()


def code_interpreter_mock_model():
    return TestModel.returning_responses(
        [
            ModelResponse(
                output=[
                    ResponseCodeInterpreterToolCall(
                        container_id="",
                        code="some code",
                        type="code_interpreter_call",
                        id="id",
                        status="completed",
                    ),
                    ResponseBuilders.response_output_message("Over 9000"),
                ],
                usage=Usage(),
                response_id=None,
            ),
        ]
    )


@workflow.defn
class CodeInterpreterWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent[str](
            name="Code Interpreter Workflow",
            instructions="You are a helpful agent.",
            tools=[
                CodeInterpreterTool(
                    tool_config={
                        "type": "code_interpreter",
                        "container": {"type": "auto"},
                    },
                )
            ],
        )
        result = await Runner.run(starting_agent=agent, input=question)

        assert any(
            isinstance(item, ToolCallItem)
            and isinstance(item.raw_item, ResponseCodeInterpreterToolCall)
            for item in result.new_items
        )
        return result.final_output


async def test_code_interpreter_tool(client: Client):
    async with AgentEnvironment(
        model=code_interpreter_mock_model(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=60),
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            CodeInterpreterWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                CodeInterpreterWorkflow.run,
                "What is the square root of273 * 312821 plus 1782?",
                id=f"code-interpreter-tool-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )
            result = await workflow_handle.result()
            assert result == "Over 9000"


def hosted_mcp_mock_model():
    return TestModel.returning_responses(
        [
            ModelResponse(
                output=[
                    McpApprovalRequest(
                        arguments="",
                        name="",
                        server_label="gitmcp",
                        type="mcp_approval_request",
                        id="id",
                    )
                ],
                usage=Usage(),
                response_id=None,
            ),
            ModelResponse(
                output=[
                    McpCall(
                        arguments="",
                        name="",
                        server_label="",
                        type="mcp_call",
                        id="id",
                        output="Mcp output",
                    ),
                    ResponseBuilders.response_output_message("Some language"),
                ],
                usage=Usage(),
                response_id=None,
            ),
        ]
    )


@workflow.defn
class HostedMCPWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        requested_approval = False

        def approve(_: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
            nonlocal requested_approval
            requested_approval = True
            return MCPToolApprovalFunctionResult(approve=True)

        agent = Agent[str](
            name="Hosted MCP Workflow",
            instructions="You are a helpful agent.",
            tools=[
                HostedMCPTool(
                    tool_config={
                        "type": "mcp",
                        "server_label": "gitmcp",
                        "server_url": "https://gitmcp.io/openai/codex",
                        "require_approval": "always",
                    },
                    on_approval_request=approve,
                )
            ],
        )
        result = await Runner.run(starting_agent=agent, input=question)
        assert requested_approval
        assert any(
            isinstance(item, ToolCallItem) and isinstance(item.raw_item, McpCall)
            for item in result.new_items
        )
        return result.final_output


async def test_hosted_mcp_tool(client: Client):
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model=hosted_mcp_mock_model(),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            HostedMCPWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                HostedMCPWorkflow.run,
                "Which language is this repo written in?",
                id=f"hosted-mcp-tool-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            result = await workflow_handle.result()
            assert result == "Some language"


class AssertDifferentModelProvider(ModelProvider):
    model_names: set[str | None]

    def __init__(self, model: Model):
        self._model = model
        self.model_names = set()

    def get_model(self, model_name: str | None) -> Model:
        self.model_names.add(model_name)
        return self._model


def multiple_models_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call("{}", "transfer_to_underling"),
            ResponseBuilders.output_message(
                "I'm here to help! Was there a specific task you needed assistance with regarding the storeroom?"
            ),
        ]
    )


@workflow.defn
class MultipleModelWorkflow:
    @workflow.run
    async def run(self, use_run_config: bool):
        underling = Agent[None](
            name="Underling",
            instructions="You do all the work you are told.",
        )

        starting_agent = Agent[None](
            name="Lazy Assistant",
            model="gpt-4o-mini",
            instructions="You delegate all your work to another agent.",
            handoffs=[underling],
        )
        result = await Runner.run(
            starting_agent=starting_agent,
            input="Have you cleaned the store room yet?",
            run_config=RunConfig(model="gpt-4o") if use_run_config else None,
        )
        return result.final_output


async def test_multiple_models(client: Client):
    provider = AssertDifferentModelProvider(multiple_models_mock_model())
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model_provider=provider,
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            MultipleModelWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                MultipleModelWorkflow.run,
                False,
                id=f"multiple-model-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            await workflow_handle.result()
            assert provider.model_names == {None, "gpt-4o-mini"}


async def test_run_config_models(client: Client):
    provider = AssertDifferentModelProvider(multiple_models_mock_model())
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model_provider=provider,
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            MultipleModelWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                MultipleModelWorkflow.run,
                True,
                id=f"run-config-model-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            await workflow_handle.result()

            # Only the model from the runconfig override is used
            assert provider.model_names == {"gpt-4o"}


async def test_summary_provider(client: Client):
    class SummaryProvider(ModelSummaryProvider):
        def provide(
            self,
            agent: Agent[Any] | None,
            instructions: str | None,
            input: str | list[TResponseInputItem],
        ) -> str:
            return "My summary"

    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120),
            summary_override=SummaryProvider(),
        ),
        model=hello_mock_model(),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            HelloWorldAgent,
        ) as worker:
            workflow_handle = await client.start_workflow(
                HelloWorldAgent.run,
                "Prompt",
                id=f"summary-provider-model-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            await workflow_handle.result()
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_scheduled_event_attributes"):
                    assert e.user_metadata.summary.data == b'"My summary"'


class OutputType(pydantic.BaseModel):
    answer: str
    model_config = ConfigDict(extra="forbid")  # Forbid additional properties


@workflow.defn
class OutputTypeWorkflow:
    @workflow.run
    async def run(self) -> OutputType:
        agent: Agent = Agent(
            name="Assistant",
            instructions="You are a helpful assistant, adhere to the json schema output",
            output_type=OutputType,
        )
        result = await Runner.run(
            starting_agent=agent,
            input="Hello!",
        )
        return result.final_output


def output_type_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.output_message(
                '{"answer": "My answer"}',
            ),
        ]
    )


async def test_output_type(client: Client):
    async with AgentEnvironment(
        model=output_type_mock_model(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120),
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            OutputTypeWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                OutputTypeWorkflow.run,
                id=f"output-type-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            result = await workflow_handle.result()
            assert isinstance(result, OutputType)
            assert result.answer == "My answer"


@workflow.defn
class McpServerWorkflow:
    @workflow.run
    async def run(self, caching: bool, factory_argument: Any | None) -> str:
        from agents.mcp import MCPServer  # type: ignore[reportUnusedImport]

        server: MCPServer = openai_agents.workflow.stateless_mcp_server(
            "HelloServer", cache_tools_list=caching, factory_argument=factory_argument
        )
        agent = Agent[str](
            name="MCP ServerWorkflow",
            instructions="Use the tools to assist the customer.",
            mcp_servers=[server],
        )
        result = await Runner.run(
            starting_agent=agent, input="Say hello to Tom and Tim."
        )
        return result.final_output


@workflow.defn
class McpServerStatefulWorkflow:
    @workflow.run
    async def run(self, timeout: timedelta, factory_argument: Any | None) -> str:
        async with openai_agents.workflow.stateful_mcp_server(
            "HelloServer",
            config=ActivityConfig(
                schedule_to_start_timeout=timeout,
                start_to_close_timeout=timedelta(seconds=30),
            ),
            factory_argument=factory_argument,
        ) as server:
            agent = Agent[str](
                name="MCP ServerWorkflow",
                instructions="Use the tools to assist the customer.",
                mcp_servers=[server],
            )
            result = await Runner.run(
                starting_agent=agent, input="Say hello to Tom and Tim."
            )
            return result.final_output


def tracking_mcp_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call(
                arguments='{"name":"Tom"}',
                name="Say-Hello",
            ),
            ResponseBuilders.tool_call(
                arguments='{"name":"Tim"}',
                name="Say-Hello",
            ),
            ResponseBuilders.output_message("Hi Tom and Tim!"),
        ]
    )


def get_tracking_server(name: str):
    from agents.mcp import MCPServer  # type: ignore
    from mcp import GetPromptResult, ListPromptsResult  # type: ignore
    from mcp import Tool as MCPTool  # type: ignore
    from mcp.types import CallToolResult, TextContent  # type: ignore

    class TrackingMCPServer(MCPServer):
        calls: list[str]

        def __init__(self, name: str):
            self._name = name
            self.calls = []
            super().__init__()

        async def connect(self):
            self.calls.append("connect")

        @property
        def name(self) -> str:
            return self._name

        async def cleanup(self):
            self.calls.append("cleanup")

        async def list_tools(
            self,
            run_context: RunContextWrapper[Any] | None = None,
            agent: AgentBase | None = None,
        ) -> list[MCPTool]:
            self.calls.append("list_tools")
            return [
                MCPTool(
                    name="Say-Hello",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                        "required": ["name"],
                        "$schema": "http://json-schema.org/draft-07/schema#",
                    },
                )
            ]

        async def call_tool(
            self, tool_name: str, arguments: dict[str, Any] | None
        ) -> CallToolResult:
            self.calls.append("call_tool")
            name = (arguments or {}).get("name") or "John Doe"
            return CallToolResult(
                content=[TextContent(type="text", text=f"Hello {name}")]
            )

        async def list_prompts(self) -> ListPromptsResult:
            raise NotImplementedError()

        async def get_prompt(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> GetPromptResult:
            raise NotImplementedError()

    return TrackingMCPServer(name)


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.parametrize("stateful", [True, False])
@pytest.mark.parametrize("caching", [True, False])
async def test_mcp_server(
    client: Client, use_local_model: bool, stateful: bool, caching: bool
):
    if not use_local_model and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No openai API key")

    if stateful and caching:
        pytest.skip("Caching is only supported for stateless MCP servers")

    from agents.mcp import MCPServer  # type: ignore

    from temporalio.contrib.openai_agents import (
        StatefulMCPServerProvider,
        StatelessMCPServerProvider,
    )

    tracking_server = get_tracking_server(name="HelloServer")
    server: StatefulMCPServerProvider | StatelessMCPServerProvider = (
        StatefulMCPServerProvider("HelloServer", lambda _: tracking_server)
        if stateful
        else StatelessMCPServerProvider("HelloServer", lambda _: tracking_server)
    )

    model = tracking_mcp_mock_model() if use_local_model else None
    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model=model,
        mcp_server_providers=[server],
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client, McpServerStatefulWorkflow, McpServerWorkflow
        ) as worker:
            if stateful:
                result = await client.execute_workflow(
                    McpServerStatefulWorkflow.run,
                    args=[timedelta(seconds=30), None],
                    id=f"mcp-server-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )
            else:
                result = await client.execute_workflow(
                    McpServerWorkflow.run,
                    args=[caching, None],
                    id=f"mcp-server-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )
        if use_local_model:
            assert result == "Hi Tom and Tim!"
    if use_local_model:
        print(tracking_server.calls)
        if stateful:
            assert tracking_server.calls == [
                "connect",
                "list_tools",
                "call_tool",
                "list_tools",
                "call_tool",
                "list_tools",
                "cleanup",
            ]
            assert len(cast(StatefulMCPServerProvider, server)._servers) == 0
        else:
            if caching:
                assert tracking_server.calls == [
                    "connect",
                    "list_tools",
                    "cleanup",
                    "connect",
                    "call_tool",
                    "cleanup",
                    "connect",
                    "call_tool",
                    "cleanup",
                ]
            else:
                assert tracking_server.calls == [
                    "connect",
                    "list_tools",
                    "cleanup",
                    "connect",
                    "call_tool",
                    "cleanup",
                    "connect",
                    "list_tools",
                    "cleanup",
                    "connect",
                    "call_tool",
                    "cleanup",
                    "connect",
                    "list_tools",
                    "cleanup",
                ]


@pytest.mark.parametrize("stateful", [True, False])
async def test_mcp_server_factory_argument(client: Client, stateful: bool):
    def factory(args: Any | None) -> MCPServer:
        print("Invoking factory: ", args)
        if args is not None:
            assert args is not None
            assert cast(dict[str, str], args).get("user") == "blah"

        return get_tracking_server("HelloServer")

    server: StatefulMCPServerProvider | StatelessMCPServerProvider = (
        StatefulMCPServerProvider("HelloServer", factory)
        if stateful
        else StatelessMCPServerProvider("HelloServer", factory)
    )

    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model=tracking_mcp_mock_model(),
        mcp_server_providers=[server],
    ) as env:
        client = env.applied_on_client(client)

        headers = {"user": "blah"}
        async with new_worker(
            client, McpServerStatefulWorkflow, McpServerWorkflow
        ) as worker:
            if stateful:
                await client.execute_workflow(
                    McpServerStatefulWorkflow.run,
                    args=[timedelta(seconds=30), headers],
                    id=f"mcp-server-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )
            else:
                await client.execute_workflow(
                    McpServerWorkflow.run,
                    args=[False, headers],
                    id=f"mcp-server-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )


async def test_stateful_mcp_server_no_worker(client: Client):
    server = StatefulMCPServerProvider(
        "Filesystem-Server",
        lambda _: MCPServerStdio(
            name="Filesystem-Server",
            params={
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    os.path.dirname(os.path.abspath(__file__)),
                ],
            },
        ),
    )

    # Override the connect activity to not actually start a worker
    @activity.defn(name="Filesystem-Server-stateful-connect")
    async def connect() -> None:
        await asyncio.sleep(30)

    def override_get_activities() -> Sequence[Callable]:
        return (connect,)

    server.get_activities = override_get_activities  # type:ignore

    async with AgentEnvironment(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        ),
        model=tracking_mcp_mock_model(),
        mcp_server_providers=[server],
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            McpServerStatefulWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                McpServerStatefulWorkflow.run,
                args=[timedelta(seconds=1), None],
                id=f"mcp-server-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            with pytest.raises(WorkflowFailureError) as err:
                await workflow_handle.result()
            assert isinstance(err.value.cause, ApplicationError)
            assert (
                err.value.cause.message
                == "MCP Stateful Server Worker failed to schedule activity."
            )


async def test_model_conversion_loops():
    agent = init_agents()
    converted = _convert_agent(ModelActivityParameters(), agent, None)
    seat_booking_handoff = converted.handoffs[1]
    assert isinstance(seat_booking_handoff, Handoff)
    context: RunContextWrapper[AirlineAgentContext] = RunContextWrapper(
        context=AirlineAgentContext()  # type: ignore
    )
    seat_booking_agent = await seat_booking_handoff.on_invoke_handoff(context, "")
    triage_agent = seat_booking_agent.handoffs[0]
    assert isinstance(triage_agent, Agent)
    assert isinstance(triage_agent.model, _TemporalModelStub)
    seat_booking_agent = await seat_booking_handoff.on_invoke_handoff(context, "")
    triage_agent = seat_booking_agent.handoffs[0]
    assert isinstance(triage_agent, Agent)
    assert isinstance(triage_agent.model, _TemporalModelStub)
    seat_booking_agent = await seat_booking_handoff.on_invoke_handoff(context, "")
    triage_agent = seat_booking_agent.handoffs[0]
    assert isinstance(triage_agent, Agent)
    assert isinstance(triage_agent.model, _TemporalModelStub)


async def test_local_hello_world_agent(client: Client):
    async with AgentEnvironment(
        model=hello_mock_model(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            use_local_activity=True,
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(client, HelloWorldAgent) as worker:
            handle = await client.start_workflow(
                HelloWorldAgent.run,
                "Tell me about recursion in programming.",
                id=f"hello-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=5),
            )
            result = await handle.result()
            assert result == "test"
            local_activity_found = False
            async for e in handle.fetch_history_events():
                if e.HasField("marker_recorded_event_attributes"):
                    local_activity_found = True
            assert local_activity_found


async def test_split_workers(client: Client):
    new_config = client.config()

    workflow_plugin = openai_agents.OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30)
        ),
        model_provider=TestModelProvider(hello_mock_model()),
        register_activities=False,
    )
    new_config["plugins"] = [workflow_plugin]
    workflow_client = Client(**new_config)

    # Workflow worker
    async with new_worker(
        workflow_client, HelloWorldAgent, no_remote_activities=True
    ) as worker:
        activity_plugin = openai_agents.OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=30)
            ),
            model_provider=TestModelProvider(hello_mock_model()),
        )
        new_config["plugins"] = [activity_plugin]
        activity_client = Client(**new_config)
        # Activity Worker
        async with new_worker(activity_client, task_queue=worker.task_queue):
            result = await activity_client.execute_workflow(
                HelloWorldAgent.run,
                "Tell me about recursion in programming.",
                id=f"hello-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            assert result == "test"


def multiple_handoffs_mock_model():
    return TestModel.returning_responses(
        [
            ResponseBuilders.tool_call("{}", "transfer_to_planner"),
            ResponseBuilders.output_message(
                "I'll analyze the requirements and create a plan."
            ),
        ]
    )


@workflow.defn
class MultipleHandoffsWorkflow:
    @workflow.run
    async def run(self, task: str) -> str:
        planner = Agent[None](
            name="Planner",
            instructions="You analyze requirements and create detailed plans.",
            handoff_description="An agent that creates detailed plans and strategies",
        )

        writer = Agent[None](
            name="Writer",
            instructions="You write documents and reports based on provided information.",
            handoff_description="An agent that writes professional documents and reports",
        )

        specialists = [planner, writer]
        handoffs_list: list[Agent[Any] | Handoff[None, Any]] = [
            handoff(agent=a) for a in specialists
        ]

        triage = Agent[None](
            name="Triage",
            instructions="Hand off to Planner when requested.",
            handoffs=handoffs_list,
        )

        result = await Runner.run(starting_agent=triage, input=task)
        return result.final_output


async def test_multiple_handoffs_workflow(client: Client):
    model = multiple_handoffs_mock_model()
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            MultipleHandoffsWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                MultipleHandoffsWorkflow.run,
                "Create a project plan for building a web application",
                id=f"multiple-handoffs-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await workflow_handle.result()

            assert result == "I'll analyze the requirements and create a plan."

            # Verify the correct handoff occurred
            events = []
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    events.append(e)

            # Should have 2 activity completions:
            # 1. Triage agent makes handoff call to planner
            # 2. Planner agent responds
            assert len(events) == 2

            # Verify handoff to planner was requested
            first_event_data = (
                events[0]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert "transfer_to_planner" in first_event_data

            # Verify that the planner agent was actually invoked (this would fail before the fix)
            planner_response_data = (
                events[1]
                .activity_task_completed_event_attributes.result.payloads[0]
                .data.decode()
            )
            assert (
                "I'll analyze the requirements and create a plan."
                in planner_response_data
            )
