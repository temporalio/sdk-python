import dataclasses
import typing
from typing import Any, Optional, Union

from agents import (
    Agent,
    AgentsException,
    Handoff,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunResult,
    RunResultStreaming,
    SQLiteSession,
    TContext,
    Tool,
    TResponseInputItem,
)
from agents.items import TResponseStreamEvent
from agents.run import DEFAULT_AGENT_RUNNER, DEFAULT_MAX_TURNS, AgentRunner
from agents.stream_events import StreamEvent

from temporalio import workflow
from temporalio.contrib.openai_agents._invoke_model_activity import (
    ActivityModelInput,
    ModelTracingInput,
)
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError


# Recursively replace models in all agents
def _convert_agent(
    model_params: ModelActivityParameters,
    agent: Agent[Any],
    seen: Optional[dict[int, Agent]],
) -> Agent[Any]:
    if seen is None:
        seen = dict()

    # Short circuit if this model was already seen to prevent looping from circular handoffs
    if id(agent) in seen:
        return seen[id(agent)]

    # This agent has already been processed in some other run
    if isinstance(agent.model, _TemporalModelStub):
        return agent

    # Save the new version of the agent so that we can replace loops
    new_agent = dataclasses.replace(agent)
    seen[id(agent)] = new_agent

    name = _model_name(agent)

    new_handoffs: list[Union[Agent, Handoff]] = []
    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            new_handoffs.append(_convert_agent(model_params, handoff, seen))
        elif isinstance(handoff, Handoff):
            original_invoke = handoff.on_invoke_handoff

            async def on_invoke(context: RunContextWrapper[Any], args: str) -> Agent:
                handoff_agent = await original_invoke(context, args)
                return _convert_agent(model_params, handoff_agent, seen)

            new_handoffs.append(
                dataclasses.replace(handoff, on_invoke_handoff=on_invoke)
            )
        else:
            raise TypeError(f"Unknown handoff type: {type(handoff)}")

    new_agent.model = _TemporalModelStub(
        model_name=name,
        model_params=model_params,
        agent=agent,
    )
    new_agent.handoffs = new_handoffs
    return new_agent


class TemporalBatchedRunResultStreaming(RunResultStreaming):
    """A RunResultStreaming that implements batched conversion in Phase I."""

    def __init__(
        self,
        runner: "TemporalOpenAIRunner",
        starting_agent: Agent[Any],
        input: Union[str, list[TResponseInputItem]],
        **kwargs,
    ):
        # Initialize with minimal data - we'll populate after the activity runs
        super().__init__(
            input=input,
            new_items=[],
            raw_responses=[],
            final_output="",
            input_guardrail_results=[],
            output_guardrail_results=[],
            context_wrapper=kwargs.get(
                "context", type("MockContext", (), {"context": None})()
            ),
            current_agent=starting_agent,
            current_turn=0,
            max_turns=kwargs.get("max_turns", DEFAULT_MAX_TURNS),
            _current_agent_output_schema=None,
            trace=None,
            is_complete=False,
        )
        self._runner = runner
        self._starting_agent = starting_agent
        self._input = input
        self._kwargs = kwargs
        self._activity_completed = False
        self._collected_events: list[dict] = []

    async def stream_events(self):
        """Stream the pre-collected events using batched conversion."""
        if not self._activity_completed:
            # Convert the agent and prepare activity input
            converted_agent = _convert_agent(
                self._runner.model_params, self._starting_agent, None
            )

            # Create activity input following the existing pattern
            activity_input = ActivityModelInput(
                model_name=_model_name(self._starting_agent),
                system_instructions=getattr(self._starting_agent, "instructions", None),
                input=self._input,
                model_settings=getattr(
                    self._starting_agent, "model_settings", ModelSettings()
                ),
                tools=[],  # Simplified for Phase I
                output_schema=None,
                handoffs=[],
                tracing=ModelTracingInput.DISABLED,
                previous_response_id=self._kwargs.get("previous_response_id"),
                conversation_id=self._kwargs.get("conversation_id"),
            )

            # Execute the streaming activity
            final_response, self._collected_events = await workflow.execute_activity(
                "invoke_model_streaming_activity",
                activity_input,
                start_to_close_timeout=self._runner.model_params.start_to_close_timeout,
                schedule_to_start_timeout=self._runner.model_params.schedule_to_start_timeout,
                schedule_to_close_timeout=self._runner.model_params.schedule_to_close_timeout,
                heartbeat_timeout=self._runner.model_params.heartbeat_timeout,
                retry_policy=self._runner.model_params.retry_policy,
            )

            # Update this instance with the final response data
            self.final_output = "Streaming complete"  # Simplified for Phase I
            self.is_complete = True
            self._activity_completed = True

        # Stream the collected events, reconstructing StreamEvent objects from dicts
        from typing import cast

        from agents.items import TResponseStreamEvent
        from agents.stream_events import RawResponsesStreamEvent

        for event_dict in self._collected_events:
            # For Phase I, all events should be raw_response_event type
            # Reconstruct RawResponsesStreamEvent from dictionary
            if event_dict.get("type") == "raw_response_event":
                # The data should be a serialized TResponseStreamEvent
                stream_event = RawResponsesStreamEvent(
                    data=cast(TResponseStreamEvent, event_dict["data"])
                )
                yield stream_event
            else:
                # For other event types in future phases, we'll implement proper handling
                # For now, skip unknown event types
                continue


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
            from temporalio.contrib.openai_agents._mcp import (
                _StatefulMCPServerReference,
                _StatelessMCPServerReference,
            )

            for s in starting_agent.mcp_servers:
                if not isinstance(
                    s,
                    (
                        _StatelessMCPServerReference,
                        _StatefulMCPServerReference,
                    ),
                ):
                    raise ValueError(
                        f"Unknown mcp_server type {type(s)} may not work durably."
                    )

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

        if run_config.model:
            if not isinstance(run_config.model, str):
                raise ValueError(
                    "Temporal workflows require a model name to be a string in the run config."
                )
            run_config = dataclasses.replace(
                run_config,
                model=_TemporalModelStub(
                    run_config.model, model_params=self.model_params, agent=None
                ),
            )

        try:
            return await self._runner.run(
                starting_agent=_convert_agent(self.model_params, starting_agent, None),
                input=input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
                previous_response_id=previous_response_id,
                session=session,
            )
        except AgentsException as e:
            # In order for workflow failures to properly fail the workflow, we need to rewrap them in
            # a Temporal error
            if e.__cause__ and workflow.is_failure_exception(e.__cause__):
                reraise = AgentsWorkflowError(
                    f"Workflow failure exception in Agents Framework: {e}"
                )
                reraise.__traceback__ = e.__traceback__
                raise reraise from e.__cause__
            else:
                raise e

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
        """Run the agent with streaming responses using batched conversion in Temporal workflows."""
        if not workflow.in_workflow():
            return self._runner.run_streamed(
                starting_agent,
                input,
                **kwargs,
            )

        # Phase I: Batched conversion implementation
        # Return a streaming result that will collect events from the streaming activity
        return TemporalBatchedRunResultStreaming(
            runner=self,
            starting_agent=starting_agent,
            input=input,
            **kwargs,
        )


def _model_name(agent: Agent[Any]) -> Optional[str]:
    name = agent.model
    if name is not None and not isinstance(name, str):
        raise ValueError(
            "Temporal workflows require a model name to be a string in the agent."
        )
    return name
