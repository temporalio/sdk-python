import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.openai_agents._openai_runner import TemporalOpenAIRunner
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    _TemporalTracingProcessor,
)
from temporalio.contrib.openai_agents.invoke_model_activity import invoke_model_activity
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from tests.helpers import new_worker

id_reuse_policy = (WorkflowIDReusePolicy.TERMINATE_IF_RUNNING,)

# Import our activity, passing it through the sandbox
with workflow.unsafe.imports_passed_through():
    from agents import Agent, RunConfig, Runner, set_trace_processors


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


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
        result = await Runner.run(agent, input=prompt)
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
        )
        print("\n--------\n", result, "\n---------\n")
