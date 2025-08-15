import json
import typing
from dataclasses import replace
from typing import Any, Union

from agents import (
    Agent,
    RunConfig,
    RunResult,
    RunResultStreaming,
    SQLiteSession,
    TContext,
    Tool,
    TResponseInputItem, Handoff, RunContextWrapper,
)
from agents.run import DEFAULT_AGENT_RUNNER, DEFAULT_MAX_TURNS, AgentRunner
from pydantic_core import to_json

from temporalio import workflow
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub


class TemporalOpenAIRunner(AgentRunner):
    """Temporal Runner for OpenAI agents.

    Forwards model calls to a Temporal activity.

    """

    def __init__(self, model_params: ModelActivityParameters) -> None:
        """Initialize the Temporal OpenAI Runner."""
        self._runner = DEFAULT_AGENT_RUNNER or AgentRunner()
        self.model_params = model_params

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        **kwargs: Any,
    ) -> RunResult:
        """Run the agent in a Temporal workflow."""
        if not workflow.in_workflow():
            return await self._runner.run(
                starting_agent,
                input,
                **kwargs,
            )

        tool_types = typing.get_args(Tool)
        for t in starting_agent.tools:
            if not isinstance(t, tool_types):
                raise ValueError(
                    "Provided tool is not a tool type. If using an activity, make sure to wrap it with openai_agents.workflow.activity_as_tool."
                )

        if starting_agent.mcp_servers:
            raise ValueError(
                "Temporal OpenAI agent does not support on demand MCP servers."
            )

        # workaround for https://github.com/pydantic/pydantic/issues/9541
        # ValidatorIterator returned
        input_json = to_json(input)
        input = json.loads(input_json)

        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = kwargs.get("hooks")
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")
        session = kwargs.get("session")

        if isinstance(session, SQLiteSession):
            raise ValueError("Temporal workflows don't support SQLite sessions.")

        if run_config is None:
            run_config = RunConfig()

        def model_name(agent: Agent[Any]) -> str:
            name = run_config.model or agent.model
            if name is not None and not isinstance(name, str):
                print("Name: ", name, " Agent: ", agent)
                raise ValueError(
                    "Temporal workflows require a model name to be a string in the run config and/or agent."
                )
            return name

        def convert_agent(agent: Agent[Any]) -> None:
            print("Model: ", agent.model)

            # Short circuit if this model was already replaced to prevent looping from circular handoffs
            if isinstance(agent.model, _TemporalModelStub):
                return

            name = model_name(agent)
            agent.model = _TemporalModelStub(
                model_name=name,
                model_params=self.model_params,
                agent=agent,
            )
            print("Model after replace: ", agent.model)

            for handoff in agent.handoffs:
                if isinstance(handoff, Agent):
                    convert_agent(handoff)
                elif isinstance(handoff, Handoff):
                    original_invoke = handoff.on_invoke_handoff
                    async def on_invoke(context: RunContextWrapper[Any], args: str) -> Agent[Any]:
                        handoff_agent = await original_invoke(context, args)
                        convert_agent(handoff_agent)
                        return handoff_agent
                    handoff.on_invoke_handoff = on_invoke

        convert_agent(starting_agent)

        return await self._runner.run(
            starting_agent=starting_agent,
            input=input,
            context=context,
            max_turns=max_turns,
            hooks=hooks,
            run_config=run_config,
            previous_response_id=previous_response_id,
            session=session,
        )

    def run_sync(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        **kwargs: Any,
    ) -> RunResult:
        """Run the agent synchronously (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_sync(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support synchronous model calls.")

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        **kwargs: Any,
    ) -> RunResultStreaming:
        """Run the agent with streaming responses (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_streamed(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support streaming.")
