from pathlib import Path

import pytest
from agents import OpenAIProvider
from openai import AsyncOpenAI

from temporalio.client import WorkflowHistory
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin
from temporalio.worker import Replayer
from tests.contrib.openai_agents.test_openai import (
    AgentsAsToolsWorkflow,
    CustomerServiceWorkflow,
    HelloWorldAgent,
    InputGuardrailWorkflow,
    OutputGuardrailWorkflow,
    ResearchWorkflow,
    ToolsWorkflow, McpServerWorkflow, McpServerStatefulWorkflow,
)


@pytest.mark.parametrize(
    "file_name",
    [
        "agents-as-tools-workflow-history.json",
        "customer-service-workflow-history.json",
        "hello-workflow-history.json",
        "input-guardrail-workflow-history.json",
        "output-guardrail-workflow-history.json",
        "research-workflow-history.json",
        "tools-workflow-history.json",
        "mcp-history-stateless.json",
        "mcp-history-stateful.json",
    ],
)
async def test_replay(file_name: str) -> None:
    with (Path(__file__).with_name("histories") / file_name).open("r") as f:
        history_json = f.read()

        await Replayer(
            workflows=[
                ResearchWorkflow,
                ToolsWorkflow,
                CustomerServiceWorkflow,
                AgentsAsToolsWorkflow,
                HelloWorldAgent,
                InputGuardrailWorkflow,
                OutputGuardrailWorkflow,
                McpServerWorkflow,
                McpServerStatefulWorkflow,
            ],
            plugins=[OpenAIAgentsPlugin()],
        ).replay_workflow(WorkflowHistory.from_json("fake", history_json))
