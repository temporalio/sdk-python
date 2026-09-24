import warnings
from pathlib import Path

import pytest

from temporalio.client import WorkflowHistory
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
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

    with warnings.catch_warnings(record=True) as recorder:
        warnings.filterwarnings(
            "always", message=r"Module .* was imported after initial workflow load"
        )
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
            plugins=[OpenAIAgentsPlugin()],
        ).replay_workflow(WorkflowHistory.from_json("fake", history_json))

    # Sandbox imports during an activation count toward the deadlock timeout
    assert not [
        str(w.message)
        for w in recorder
        if "was imported after initial workflow load" in str(w.message)
    ]
