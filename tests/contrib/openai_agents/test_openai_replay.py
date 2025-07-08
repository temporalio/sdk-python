from pathlib import Path

import pytest

from temporalio.client import WorkflowHistory
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer
from tests.contrib.openai_agents.test_openai import (
    AgentsAsToolsWorkflow,
    CustomerServiceWorkflow,
    HelloWorldAgent,
    InputGuardrailWorkflow,
    OutputGuardrailWorkflow,
    ResearchWorkflow,
    ToolsWorkflow,
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
    ],
)
async def test_replay(file_name: str) -> None:
    with (Path(__file__).with_name("histories") / file_name).open("r") as f:
        history_json = f.read()

        with set_open_ai_agent_temporal_overrides():
            await Replayer(
                workflows=[
                    ResearchWorkflow,
                    ToolsWorkflow,
                    CustomerServiceWorkflow,
                    AgentsAsToolsWorkflow,
                    HelloWorldAgent,
                    InputGuardrailWorkflow,
                    OutputGuardrailWorkflow,
                ],
                data_converter=pydantic_data_converter,
            ).replay_workflow(WorkflowHistory.from_json("fake", history_json))
