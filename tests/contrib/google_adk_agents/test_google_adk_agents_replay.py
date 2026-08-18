from pathlib import Path

import pytest
from google.adk.models import LLMRegistry

from temporalio.client import WorkflowHistory
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin
from temporalio.worker import Replayer
from tests.contrib.google_adk_agents.test_adk_graph_workflows import (
    SequentialGraphWorkflow,
)
from tests.contrib.google_adk_agents.test_adk_hitl import HumanInputGraphWorkflow
from tests.contrib.google_adk_agents.test_google_adk_agents import (
    MultiAgentWorkflow,
    ResearchModel,
    WeatherAgent,
    WeatherModel,
)


@pytest.mark.parametrize(
    "file_name",
    [
        "multi_agent.json",
        "single_agent.json",
        "graph_workflow.json",
        "hitl_workflow.json",
    ],
)
async def test_replay(file_name: str) -> None:
    with (Path(__file__).with_name("histories") / file_name).open("r") as f:
        history_json = f.read()

        LLMRegistry.register(ResearchModel)
        LLMRegistry.register(WeatherModel)
        await Replayer(
            workflows=[
                MultiAgentWorkflow,
                WeatherAgent,
                SequentialGraphWorkflow,
                HumanInputGraphWorkflow,
            ],
            plugins=[GoogleAdkPlugin()],
        ).replay_workflow(WorkflowHistory.from_json("fake", history_json))
