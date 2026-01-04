"""End-to-end tests for LangGraph Functional API integration.

These tests verify the complete functional API integration with Temporal.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, activity_options
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    simple_functional_entrypoint,
)
from tests.contrib.langgraph.e2e_functional_workflows import (
    SimpleFunctionalE2EWorkflow,
)
from tests.helpers import new_worker


class TestFunctionalAPIBasicExecution:
    """Tests for basic functional API execution."""

    @pytest.mark.asyncio
    async def test_simple_functional_entrypoint(self, client: Client) -> None:
        """Test basic functional API entrypoint execution.

        This test verifies that:
        1. The unified plugin correctly registers entrypoints
        2. compile works inside a workflow
        3. @task functions are executed as activities
        4. The result is returned correctly

        Expected: input 10 -> double (20) -> add 10 (30) -> result: 30
        """
        plugin = LangGraphPlugin(
            graphs={"e2e_simple_functional": simple_functional_entrypoint},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30)
            ),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, SimpleFunctionalE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                SimpleFunctionalE2EWorkflow.run,
                10,
                id=f"e2e-functional-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # 10 * 2 = 20, 20 + 10 = 30
            assert result["result"] == 30
