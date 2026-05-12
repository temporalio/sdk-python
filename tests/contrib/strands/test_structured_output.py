from uuid import uuid4

from pydantic import BaseModel, Field
from strands import Agent

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.mock_model import MockModel


class PersonInfo(BaseModel):
    name: str = Field(description="Name of the person")
    age: int = Field(description="Age of the person")
    occupation: str = Field(description="Occupation of the person")


@workflow.defn
class StructuredOutputWorkflow:
    def __init__(self) -> None:
        model = MockModel(
            [
                {
                    "name": "PersonInfo",
                    "input": {
                        "name": "John Smith",
                        "age": 30,
                        "occupation": "software engineer",
                    },
                },
            ]
        )
        self.agent = Agent(model=model, structured_output_model=PersonInfo)

    @workflow.run
    async def run(self, prompt: str) -> PersonInfo:
        result = await self.agent.invoke_async(prompt)
        assert isinstance(result.structured_output, PersonInfo)
        return result.structured_output


async def test_structured_output(client: Client):
    task_queue = "test_structured_output"
    plugin = StrandsPlugin()

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[StructuredOutputWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            StructuredOutputWorkflow.run,
            "John Smith is a 30 year-old software engineer",
            id=f"test_structured_output_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == PersonInfo(
            name="John Smith", age=30, occupation="software engineer"
        )

    await Replayer(
        workflows=[StructuredOutputWorkflow],
        plugins=[plugin],
    ).replay_workflow(await handle.fetch_history())
