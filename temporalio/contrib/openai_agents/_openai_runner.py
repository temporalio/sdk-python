from dataclasses import replace
from typing import Union

from agents import (
    Agent,
    RunConfig,
    RunHooks,
    Runner,
    RunResult,
    RunResultStreaming,
    TContext,
    TResponseInputItem,
)
from agents.run import DEFAULT_MAX_TURNS, DEFAULT_RUNNER, DefaultRunner

from temporalio import workflow
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub

# TODO: Uncomment when Agent.tools type accepts Callable
# def _activities_as_tools(tools: list[Tool]) -> list[Tool]:
#     """Convert activities to tools."""
#     return [activity_as_tool(tool) if isinstance(tool, Callable) else tool for tool in tools]


class TemporalOpenAIRunner(Runner):
    """Temporal Runner for OpenAI agents.

    Forwards model calls to a Temporal activity.

    """

    def __init__(self):
        """Initialize the Temporal OpenAI Runner."""
        self._runner = DEFAULT_RUNNER or DefaultRunner()

    async def _run_impl(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        *,
        context: Union[TContext, None] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: Union[RunHooks[TContext], None] = None,
        run_config: Union[RunConfig, None] = None,
        previous_response_id: Union[str, None] = None,
    ) -> RunResult:
        """Run the agent in a Temporal workflow."""
        if not workflow.in_workflow():
            return await self._runner._run_impl(
                starting_agent,
                input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
            )
        if run_config is None:
            run_config = RunConfig()

        if run_config.model is not None and not isinstance(run_config.model, str):
            raise ValueError(
                "Temporal workflows require a model name to be a string in the run config."
            )
        updated_run_config = replace(
            run_config, model=_TemporalModelStub(run_config.model)
        )

        # TODO: Uncomment when Agent.tools type accepts Callable
        # tools = _activities_as_tools(starting_agent.tools) if starting_agent.tools else None
        # updated_starting_agent = replace(starting_agent, tools=tools)

        with workflow.unsafe.imports_passed_through():
            return await self._runner._run_impl(
                starting_agent=starting_agent,
                input=input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=updated_run_config,
                previous_response_id=previous_response_id,
            )

    def _run_sync_impl(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        *,
        context: Union[TContext, None] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: Union[RunHooks[TContext], None] = None,
        run_config: Union[RunConfig, None] = None,
        previous_response_id: Union[str, None] = None,
    ) -> RunResult:
        if not workflow.in_workflow():
            return self._runner._run_sync_impl(
                starting_agent,
                input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
            )
        raise RuntimeError("Temporal workflows do not support synchronous model calls.")

    def _run_streamed_impl(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        context: Union[TContext, None] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        hooks: Union[RunHooks[TContext], None] = None,
        run_config: Union[RunConfig, None] = None,
        previous_response_id: Union[str, None] = None,
    ) -> RunResultStreaming:
        if not workflow.in_workflow():
            return self._runner._run_streamed_impl(
                starting_agent,
                input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
            )
        raise RuntimeError("Temporal workflows do not support streaming.")
