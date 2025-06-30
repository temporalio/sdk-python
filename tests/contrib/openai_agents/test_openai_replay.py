from datetime import timedelta
from pathlib import Path

import pytest

from temporalio.client import WorkflowHistory
from temporalio.contrib.openai_agents.model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents.temporal_openai_agents import (
    set_open_ai_agent_temporal_overrides,
)
from temporalio.worker import Replayer
from tests.contrib.openai_agents.test_openai import (
    ResearchWorkflow,
    ToolsWorkflow,
    CustomerServiceWorkflow,
    AgentsAsToolsWorkflow,
    HelloWorldAgent,
)


@pytest.mark.parametrize(
    "file_name",
    [
        "agents-as-tools-workflow-history.json",
        "customer-service-workflow-history.json",
        "hello-workflow-history.json",
        "research-workflow-history.json",
        "tools-workflow-history.json",
    ],
)
async def test_replay(file_name: str) -> None:
    with (Path(__file__).with_name("histories") / file_name).open("r") as f:
        history_json = f.read()

        model_params = ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120)
        )
        with set_open_ai_agent_temporal_overrides(model_params):
            await Replayer(
                workflows=[
                    ResearchWorkflow,
                    ToolsWorkflow,
                    CustomerServiceWorkflow,
                    AgentsAsToolsWorkflow,
                    HelloWorldAgent,
                ]
            ).replay_workflow(WorkflowHistory.from_json("fake", history_json))
