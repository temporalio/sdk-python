import uuid
from datetime import timedelta
from typing import Union

from agents import (
    Agent,
    AgentOutputSchemaBase,
    Handoff,
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
from openai import AsyncOpenAI
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents.invoke_model_activity import invoke_model_activity
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from tests.helpers import new_worker


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
        workflow.logger.warning("Creating agent")
        agent = Agent(
            name="Assistant",
            instructions="You only respond in haikus.",
        )  # type: Agent
        workflow.logger.warning("Created agent")
        MultiProvider.get_model = lambda self, name: TestModel(  # type: ignore
            name or "", openai_client=AsyncOpenAI()
        )
        config = RunConfig(model="test_model")
        result = await Runner.run(agent, input=prompt, run_config=config)
        workflow.logger.warning("Run result")
        return result.final_output


async def test_hello_world_agent(client: Client):
    set_open_ai_agent_temporal_overrides()
    async with new_worker(
        client, HelloWorldAgent, activities=[invoke_model_activity]
    ) as worker:
        result = await client.execute_workflow(
            HelloWorldAgent.run,
            "Tell me about recursion in programming.",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=5),
        )
        assert result == "test"
