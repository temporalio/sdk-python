"""Sub-agents inherit the activity seams.

Deep Agents builds sub-agents as separate graphs, but they inherit the parent's
``model`` object and tools by default. Because the plugin substitutes the model
*object*, every sub-agent's model call routes through an activity without any
per-sub-agent wiring. This runs a real agent configured with a sub-agent and
asserts model calls still land on the activity seam.

The exact number of hops depends on ``deepagents`` internals we do not pin, so
the assertion is the robust invariant: the configured agent runs and at least one
model call went through an activity.
"""

# The deepagents / langchain optional deps cannot install on Python 3.10
# (deepagents pins >=3.11), so pyright cannot resolve their imports there;
# runtime collection is guarded by importorskip below.
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import sys
import uuid

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from deepagents import create_deep_agent

    from temporalio.contrib.deepagents import DeepAgentsPlugin
    from temporalio.contrib.deepagents.testing import mock_model_provider

INVOKE_MODEL = "deepagents.invoke_model"


@workflow.defn
class SubAgentWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            system_prompt="You coordinate research.",
            subagents=[
                {
                    "name": "researcher",
                    "description": "Researches a topic in depth.",
                    "system_prompt": "You research topics.",
                }
            ],
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        return result["messages"][-1].content


@pytest.mark.asyncio
async def test_subagent_calls_route_to_activities(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["Coordinated answer."]),
    )
    async with Worker(
        env.client,
        task_queue="da-subagent",
        workflows=[SubAgentWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            SubAgentWorkflow.run,
            "Investigate the topic.",
            id=f"da-subagent-{uuid.uuid4()}",
            task_queue="da-subagent",
        )
        out = await handle.result()

    assert out
    counts = await count_scheduled_activities(handle)
    # A model instance shared with the sub-agent means model calls are activities.
    assert counts[INVOKE_MODEL] >= 1, counts
