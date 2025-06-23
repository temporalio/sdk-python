from dataclasses import replace
from datetime import timedelta
from typing import Optional, Union

from agents import (
    Agent,
    RunConfig,
    RunHooks,
    RunResult,
    RunResultStreaming,
    TContext,
    TResponseInputItem,
)
from agents.run import DEFAULT_AGENT_RUNNER, DEFAULT_MAX_TURNS, AgentRunner

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.openai_agents._temporal_model_stub import _TemporalModelStub
from temporalio.workflow import ActivityCancellationType, VersioningIntent


class TemporalOpenAIRunner(AgentRunner):
    """Temporal Runner for OpenAI agents.

    Forwards model calls to a Temporal activity.

    """

    def __init__(
        self,
        *,
        task_queue: Optional[str] = None,
        schedule_to_close_timeout: Optional[timedelta] = None,
        schedule_to_start_timeout: Optional[timedelta] = None,
        start_to_close_timeout: Optional[timedelta] = None,
        heartbeat_timeout: Optional[timedelta] = None,
        retry_policy: Optional[RetryPolicy] = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        activity_id: Optional[str] = None,
        versioning_intent: Optional[VersioningIntent] = None,
        summary: Optional[str] = None,
        priority: Priority = Priority.default,
    ) -> None:
        """Initialize the Temporal OpenAI Runner."""
        self._runner = DEFAULT_AGENT_RUNNER or AgentRunner()
        self.task_queue = task_queue
        self.schedule_to_close_timeout = schedule_to_close_timeout
        self.schedule_to_start_timeout = schedule_to_start_timeout
        self.start_to_close_timeout = start_to_close_timeout
        self.heartbeat_timeout = heartbeat_timeout
        self.retry_policy = retry_policy
        self.cancellation_type = cancellation_type
        self.activity_id = activity_id
        self.versioning_intent = versioning_intent
        self.summary = summary
        self.priority = priority

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        **kwargs,
    ) -> RunResult:
        """Run the agent in a Temporal workflow."""
        if not workflow.in_workflow():
            return await self._runner.run(
                starting_agent,
                input,
                **kwargs,
            )

        context = kwargs.get("context")
        max_turns = kwargs.get("max_turns", DEFAULT_MAX_TURNS)
        hooks = kwargs.get("hooks")
        run_config = kwargs.get("run_config")
        previous_response_id = kwargs.get("previous_response_id")

        if run_config is None:
            run_config = RunConfig()

        if run_config.model is not None and not isinstance(run_config.model, str):
            raise ValueError(
                "Temporal workflows require a model name to be a string in the run config."
            )
        updated_run_config = replace(
            run_config,
            model=_TemporalModelStub(
                run_config.model,
                task_queue=self.task_queue,
                schedule_to_close_timeout=self.schedule_to_close_timeout,
                schedule_to_start_timeout=self.schedule_to_start_timeout,
                start_to_close_timeout=self.start_to_close_timeout,
                heartbeat_timeout=self.heartbeat_timeout,
                retry_policy=self.retry_policy,
                cancellation_type=self.cancellation_type,
                activity_id=self.activity_id,
                versioning_intent=self.versioning_intent,
                summary=self.summary,
                priority=self.priority,
            ),
        )

        with workflow.unsafe.imports_passed_through():
            return await self._runner.run(
                starting_agent=starting_agent,
                input=input,
                context=context,
                max_turns=max_turns,
                hooks=hooks,
                run_config=updated_run_config,
                previous_response_id=previous_response_id,
            )

    def run_sync(
        self,
        starting_agent: Agent[TContext],
        input: Union[str, list[TResponseInputItem]],
        **kwargs,
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
        **kwargs,
    ) -> RunResultStreaming:
        """Run the agent with streaming responses (not supported in Temporal workflows)."""
        if not workflow.in_workflow():
            return self._runner.run_streamed(
                starting_agent,
                input,
                **kwargs,
            )
        raise RuntimeError("Temporal workflows do not support streaming.")
