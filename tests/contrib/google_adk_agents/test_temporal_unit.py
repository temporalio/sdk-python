# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for Temporal integration helpers."""

import asyncio
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import types

# Configure Mocks globally
# We create fresh mocks here.
mock_workflow = MagicMock()
mock_activity = MagicMock()
mock_worker = MagicMock()
mock_client = MagicMock()
mock_converter = MagicMock()

# Important: execute_activity must be awaitable
mock_workflow.execute_activity = AsyncMock(return_value="mock_result")
mock_workflow.in_workflow = MagicMock(return_value=False)
mock_workflow.now = MagicMock()
mock_workflow.uuid4 = MagicMock()

# Mock the parent package
mock_temporalio = MagicMock()
mock_temporalio.workflow = mock_workflow
mock_temporalio.activity = mock_activity
mock_temporalio.worker = mock_worker
mock_temporalio.client = mock_client
mock_temporalio.converter = mock_converter


class FakeSimplePlugin:
    def __init__(self, **kwargs):
        pass


mock_temporalio.plugin = MagicMock()
mock_temporalio.plugin.SimplePlugin = FakeSimplePlugin
mock_temporalio.worker.workflow_sandbox = MagicMock()
mock_temporalio.contrib = MagicMock()
mock_temporalio.contrib.pydantic = MagicMock()

# Mock sys.modules
# Mock sys.modules
# We must ensure we get a fresh import of 'google.adk.integrations.temporal'
# that uses our MOCKED 'temporalio'.
# If it was already loaded, we remove it.
for mod in list(sys.modules.keys()):
    if mod.startswith("google.adk") or mod == "temporalio":
        del sys.modules[mod]

with patch.dict(
    sys.modules,
    {
        "temporalio": mock_temporalio,
        "temporalio.workflow": mock_workflow,
        "temporalio.activity": mock_activity,
        "temporalio.worker": mock_worker,
        "temporalio.client": mock_client,
        "temporalio.converter": mock_converter,
        "temporalio.common": MagicMock(),
        "temporalio.plugin": mock_temporalio.plugin,
        "temporalio.worker.workflow_sandbox": mock_temporalio.worker.workflow_sandbox,
        "temporalio.contrib": mock_temporalio.contrib,
        "temporalio.contrib.pydantic": mock_temporalio.contrib.pydantic,
    },
):
    from google.adk import runtime
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.models import LlmRequest, LlmResponse

    from temporalio.contrib import google_adk_agents as temporal


class TestTemporalIntegration(unittest.TestCase):
    def test_activity_as_tool_wrapper(self):
        # Reset mocks
        mock_workflow.reset_mock()
        mock_workflow.execute_activity = AsyncMock(return_value="mock_result")

        # Verify mock setup
        assert temporal.workflow.execute_activity is mock_workflow.execute_activity

        # Define a fake activity
        async def my_activity(arg: str) -> str:
            """My Docstring."""
            return f"Hello {arg}"

        # Wrap it
        tool = temporal.AgentPlugin.activity_tool(
            my_activity, start_to_close_timeout=100
        )

        # Check metadata
        self.assertEqual(tool.__name__, "my_activity")  # Matches function name
        self.assertEqual(tool.__doc__, "My Docstring.")

        # Run tool (wrapper)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(tool(arg="World"))
        finally:
            loop.close()

        # Verify call
        mock_workflow.execute_activity.assert_called_once()
        args, kwargs = mock_workflow.execute_activity.call_args
        self.assertEqual(args[1], "World")
        self.assertEqual(kwargs["start_to_close_timeout"], 100)

    def test_temporal_plugin_before_model(self):
        plugin = temporal.AgentPlugin(activity_options={"start_to_close_timeout": 60})

        # Setup mocks
        mock_workflow.reset_mock()
        mock_workflow.in_workflow.return_value = True
        response_content = types.Content(parts=[types.Part(text="plugin_resp")])
        llm_response = LlmResponse(content=response_content)
        # The plugin now expects the activity to return dicts (model_dump(mode='json'))
        # to ensure safe deserialization across process boundaries.
        response_dict = llm_response.model_dump(mode="json", by_alias=True)
        # Ensure 'content' key is present and correct (pydantic dump might be complex)
        # For the test simple case, the dump is sufficient.

        mock_workflow.execute_activity = AsyncMock(return_value=[response_dict])

        # callback_context = MagicMock(spec=CallbackContext)
        # Using spec might hide dynamic attributes or properties if not fully mocked
        callback_context = MagicMock()
        callback_context.agent_name = "test-agent"
        callback_context.invocation_context.agent.model = "test-agent-model"

        llm_request = LlmRequest(model="test-agent-model", prompt="hi")

        # Run callback
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                plugin.before_model_callback(
                    callback_context=callback_context, llm_request=llm_request
                )
            )
        finally:
            loop.close()

        # Verify execution
        mock_workflow.execute_activity.assert_called_once()
        args, kwargs = mock_workflow.execute_activity.call_args
        self.assertEqual(kwargs["start_to_close_timeout"], 60)

        # Check dynamic activity name
        self.assertEqual(args[0], "test-agent.generate_content")
        self.assertEqual(kwargs["args"][0].model, "test-agent-model")

        # Verify result merge
        self.assertIsNotNone(result)
        # Result is re-hydrated LlmResponse
        self.assertEqual(result.content.parts[0].text, "plugin_resp")
