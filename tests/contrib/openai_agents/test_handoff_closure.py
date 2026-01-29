"""Test to verify the handoff closure bug fix.

This test reproduces the bug from https://github.com/openai/openai-agents-python/issues/2216
where all handoffs would route to the last agent because of Python's closure late-binding behavior.
"""

from datetime import timedelta
from typing import Any

import pytest
from agents import Agent, Handoff, RunContextWrapper, handoff

from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._openai_runner import _convert_agent


@pytest.mark.asyncio
async def test_convert_agent_handoffs_route_correctly():
    """Test that each handoff routes to the correct agent after conversion.

    This test verifies the fix for the closure late-binding bug where
    all handoffs would incorrectly route to the last agent in the list.
    """
    # Create multiple agents
    agent_manager = Agent(name="Manager")
    agent_planner = Agent(name="Planner")
    agent_designer = Agent(name="Designer")
    agent_content_creator = Agent(name="ContentCreator")
    agent_small_edits = Agent(name="SmallEdits")

    # Set up handoffs from manager to all other agents
    agent_manager.handoffs = [
        handoff(agent=agent_planner),
        handoff(agent=agent_designer),
        handoff(agent=agent_content_creator),
        handoff(agent=agent_small_edits),
    ]

    # Convert the agent (this is where the bug occurred)
    model_params = ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30))
    converted_agent = _convert_agent(model_params, agent_manager, None)

    # Verify each handoff routes to the correct agent
    ctx: RunContextWrapper[Any] = RunContextWrapper(None)
    expected_names = ["Planner", "Designer", "ContentCreator", "SmallEdits"]

    for h, expected_name in zip(converted_agent.handoffs, expected_names):
        assert isinstance(h, Handoff), f"Expected Handoff, got {type(h)}"
        result_agent = await h.on_invoke_handoff(ctx, "")
        assert result_agent.name == expected_name, (
            f"Handoff '{h.tool_name}' should route to '{expected_name}' "
            f"but routed to '{result_agent.name}'"
        )


@pytest.mark.asyncio
async def test_convert_agent_single_handoff():
    """Test that a single handoff still works correctly."""
    agent_a = Agent(name="AgentA")
    agent_b = Agent(name="AgentB")

    agent_a.handoffs = [handoff(agent=agent_b)]

    model_params = ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30))
    converted_agent = _convert_agent(model_params, agent_a, None)

    ctx: RunContextWrapper[Any] = RunContextWrapper(None)
    h = converted_agent.handoffs[0]
    assert isinstance(h, Handoff)
    result_agent = await h.on_invoke_handoff(ctx, "")
    assert result_agent.name == "AgentB"


@pytest.mark.asyncio
async def test_convert_agent_mixed_handoffs():
    """Test conversion with both Agent and Handoff types in the handoffs list."""
    agent_manager = Agent(name="Manager")
    agent_a = Agent(name="AgentA")
    agent_b = Agent(name="AgentB")
    agent_c = Agent(name="AgentC")

    # Mix of Agent and handoff() in the handoffs list
    agent_manager.handoffs = [
        agent_a,  # Direct agent reference
        handoff(agent=agent_b),  # Handoff wrapper
        agent_c,  # Direct agent reference
    ]

    model_params = ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30))
    converted_agent = _convert_agent(model_params, agent_manager, None)

    # The handoffs list should have been converted properly
    assert len(converted_agent.handoffs) == 3

    # Direct Agent references should have been converted and kept as Agents
    assert isinstance(converted_agent.handoffs[0], Agent)
    assert converted_agent.handoffs[0].name == "AgentA"

    # Handoff wrapper should still work correctly
    assert isinstance(converted_agent.handoffs[1], Handoff)
    ctx: RunContextWrapper[Any] = RunContextWrapper(None)
    result_agent = await converted_agent.handoffs[1].on_invoke_handoff(ctx, "")
    assert result_agent.name == "AgentB"

    # Direct Agent reference
    assert isinstance(converted_agent.handoffs[2], Agent)
    assert converted_agent.handoffs[2].name == "AgentC"
