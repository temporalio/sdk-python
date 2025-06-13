import json
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Union, Optional

import pytest

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.openai_agents.invoke_model_activity import (
    ModelActivity,
)
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from temporalio.contrib.openai_agents.temporal_tools import activity_as_tool
from tests.helpers import new_worker

with workflow.unsafe.imports_passed_through():
    from agents import (
        Agent,
        AgentOutputSchemaBase,
        Handoff,
        ItemHelpers,
        MessageOutputItem,
        Model,
        ModelProvider,
        ModelResponse,
        ModelSettings,
        ModelTracing,
        OpenAIResponsesModel,
        RunContextWrapper,
        Runner,
        Tool,
        TResponseInputItem,
        Usage,
        function_tool,
        handoff,
        trace,
    )
    from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
    from agents.items import (
        HandoffOutputItem,
        ToolCallItem,
        ToolCallOutputItem,
    )
    from openai import AsyncOpenAI, BaseModel
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseFunctionWebSearch,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from tests.contrib.research_agents.research_manager import ResearchManager


class TestProvider(ModelProvider):
    __test__ = False

    def __init__(self, model: Model):
        self._model = model

    def get_model(self, model_name: Union[str, None]) -> Model:
        return self._model


response_index: int = 0


class TestModel(OpenAIResponsesModel):
    __test__ = False
    responses: list[ModelResponse] = []

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
    ) -> None:
        global response_index
        response_index = 0
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


class TestHelloModel(TestModel):
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
        agent = Agent(
            name="Assistant",
            instructions="You only respond in haikus.",
        )  # type: Agent
        result = await Runner.run(agent, input=prompt)
        return result.final_output


async def test_hello_world_agent(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    with set_open_ai_agent_temporal_overrides():
        model_activity = ModelActivity(
            TestProvider(
                TestHelloModel(  # type: ignore
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


class TestWeatherModel(TestModel):
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


@workflow.defn
class ToolsWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = Agent(
            name="Hello world",
            instructions="You are a helpful agent.",
            tools=[activity_as_tool(get_weather)],
        )  # type: Agent
        result = await Runner.run(agent, input=question)
        return result.final_output


async def test_tool_workflow(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    with set_open_ai_agent_temporal_overrides():
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


class TestPlannerModel(OpenAIResponsesModel):
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


class TestReportModel(OpenAIResponsesModel):
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
                            text="report",
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


class TestResearchModel(TestModel):
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
                        id="", status="completed", type="web_search_call"
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


async def test_research_workflow(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    global response_index
    response_index = 0

    with set_open_ai_agent_temporal_overrides():
        model_activity = ModelActivity(
            TestProvider(
                TestResearchModel(  # type: ignore
                    "", openai_client=AsyncOpenAI(api_key="Fake key")
                )
            )
        )
        async with new_worker(
            client,
            ResearchWorkflow,
            activities=[model_activity.invoke_model_activity, get_weather],
        ) as worker:
            workflow_handle = await client.start_workflow(
                ResearchWorkflow.run,
                "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )
            result = await workflow_handle.result()
            activity_count = 0
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    activity_count += 1
            assert activity_count == 12
            assert result == "report"


def orchestrator_agent() -> Agent:
    spanish_agent = Agent(
        name="spanish_agent",
        instructions="You translate the user's message to Spanish",
        handoff_description="An english to spanish translator",
    )  # type: Agent

    french_agent = Agent(
        name="french_agent",
        instructions="You translate the user's message to French",
        handoff_description="An english to french translator",
    )  # type: Agent

    italian_agent = Agent(
        name="italian_agent",
        instructions="You translate the user's message to Italian",
        handoff_description="An english to italian translator",
    )  # type: Agent

    orchestrator_agent = Agent(
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
    )  # type: Agent
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

            orchestrator_result = await Runner.run(orchestrator, msg)

            for item in orchestrator_result.new_items:
                if isinstance(item, MessageOutputItem):
                    text = ItemHelpers.text_message_output(item)
                    if text:
                        print(f"  - Translation step: {text}")

            synthesizer_result = await Runner.run(
                synthesizer, orchestrator_result.to_input_list()
            )

        return synthesizer_result.final_output


class AgentAsToolsModel(TestModel):
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


async def test_agents_as_tools_workflow(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    with set_open_ai_agent_temporal_overrides():
        model_activity = ModelActivity(
            TestProvider(
                AgentAsToolsModel(  # type: ignore
                    "", openai_client=AsyncOpenAI(api_key="Fake key")
                )
            )
        )
        async with new_worker(
            client,
            AgentsAsToolsWorkflow,
            activities=[model_activity.invoke_model_activity],
        ) as worker:
            workflow_handle = await client.start_workflow(
                AgentsAsToolsWorkflow.run,
                "Translate to Spanish: 'I am full'",
                id=f"agents-as-tools-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await workflow_handle.result()
            activity_count = 0
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    activity_count += 1
            assert activity_count == 4
            assert result == 'The translation to Spanish is: "Estoy lleno."'


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
    """
    Update the seat for a given confirmation number.

    Args:
        confirmation_number: The confirmation number for the flight.
        new_seat: The new seat to update to.
    """
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


class CustomerServiceModel(TestModel):
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
                self.current_agent, self.input_items, context=self.context
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


async def test_customer_service_workflow(client: Client):
    if sys.version_info < (3, 11):
        pytest.skip("Open AI support has type errors on 3.9")

    questions = ["Hello", "Book me a flight to PDX", "11111", "Any window seat"]

    with set_open_ai_agent_temporal_overrides():
        model_activity = ModelActivity(
            TestProvider(
                CustomerServiceModel(  # type: ignore
                    "", openai_client=AsyncOpenAI(api_key="Fake key")
                )
            )
        )
        async with new_worker(
            client,
            CustomerServiceWorkflow,
            activities=[model_activity.invoke_model_activity],
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

            with pytest.raises(WorkflowFailureError):
                await workflow_handle.result()

            activity_count = 0
            async for e in workflow_handle.fetch_history_events():
                if e.HasField("activity_task_completed_event_attributes"):
                    activity_count += 1

            assert activity_count == 6
