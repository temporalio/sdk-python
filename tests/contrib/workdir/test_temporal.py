"""Tests for the @workspace Temporal decorator."""

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.contrib.workdir import get_workspace_path, workspace
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@dataclass
class ComponentInput:
    """Input for test activities."""

    component_name: str
    data: str


@workspace(
    "memory://temporal-test/{workflow_id}/{component}",
    key_fn=lambda input: {"component": input.component_name},
)
@activity.defn
async def write_component(input: ComponentInput) -> str:
    """Activity that writes data to a workspace."""
    ws = get_workspace_path()
    (ws / "component.json").write_text(
        json.dumps({"name": input.component_name, "data": input.data})
    )
    return f"wrote {input.component_name}"


@workspace(
    "memory://temporal-test/{workflow_id}/{component}",
    key_fn=lambda input: {"component": input.component_name},
)
@activity.defn
async def read_component(input: ComponentInput) -> str:
    """Activity that reads data from a workspace."""
    ws = get_workspace_path()
    content = json.loads((ws / "component.json").read_text())
    return content["data"]


@workflow.defn
class WriteReadWorkflow:
    """Workflow that writes then reads from a workspace."""

    @workflow.run
    async def run(self, component_name: str, data: str) -> str:
        input_obj = ComponentInput(component_name=component_name, data=data)

        await workflow.execute_activity(
            write_component,
            input_obj,
            start_to_close_timeout=timedelta(seconds=30),
        )

        return await workflow.execute_activity(
            read_component,
            input_obj,
            start_to_close_timeout=timedelta(seconds=30),
        )


class TestWorkspaceDecorator:
    """Tests for the @workspace decorator with real Temporal workers."""

    @pytest.fixture
    async def env(self) -> WorkflowEnvironment:
        """Start a local Temporal test environment."""
        return await WorkflowEnvironment.start_local()

    async def test_write_then_read(self, env: WorkflowEnvironment) -> None:
        """Data written in one activity is readable in another."""
        task_queue = str(uuid.uuid4())

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[WriteReadWorkflow],
            activities=[write_component, read_component],
        ):
            result = await env.client.execute_workflow(
                WriteReadWorkflow.run,
                args=["my-component", "hello-world"],
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )

        assert result == "hello-world"
