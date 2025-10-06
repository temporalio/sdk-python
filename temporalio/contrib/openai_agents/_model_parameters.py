"""Parameters for configuring Temporal activity execution for model calls."""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional, Union

from agents import Agent, TResponseInputItem

from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent


class ActivityExecutionMode(enum.Enum):
    """Mode for executing model invocation activities in workflows.

    This controls how the OpenAI Agents SDK's streaming functionality is integrated
    with Temporal activities when using Runner.run_streamed().

    - BATCH_ACTIVITY: Collects all streaming events in the activity, returns as a batch
    - STREAMING_ACTIVITY_NON_PEEKABLE: Streaming activity with peekable=False (batched replay)
    - STREAMING_ACTIVITY_PEEKABLE: Streaming activity with peekable=True (real-time events)

    Note: Runner.run() (non-streaming) always uses a separate non-streaming activity path.
    """

    BATCH_ACTIVITY = "batch_activity"
    STREAMING_ACTIVITY_NON_PEEKABLE = "streaming_activity_non_peekable"
    STREAMING_ACTIVITY_PEEKABLE = "streaming_activity_peekable"


class ModelSummaryProvider(ABC):
    """Abstract base class for providing model summaries. Essentially just a callable,
    but the arguments are sufficiently complex to benefit from names.
    """

    @abstractmethod
    def provide(
        self,
        agent: Optional[Agent[Any]],
        instructions: Optional[str],
        input: Union[str, list[TResponseInputItem]],
    ) -> str:
        """Given the provided information, produce a summary for the model invocation activity."""
        pass


@dataclass
class ModelActivityParameters:
    """Parameters for configuring Temporal activity execution for model calls.

    This class encapsulates all the parameters that can be used to configure
    how Temporal activities are executed when making model calls through the
    OpenAI Agents integration.
    """

    task_queue: Optional[str] = None
    """Specific task queue to use for model activities."""

    schedule_to_close_timeout: Optional[timedelta] = None
    """Maximum time from scheduling to completion."""

    schedule_to_start_timeout: Optional[timedelta] = None
    """Maximum time from scheduling to starting."""

    start_to_close_timeout: Optional[timedelta] = timedelta(seconds=60)
    """Maximum time for the activity to complete."""

    heartbeat_timeout: Optional[timedelta] = None
    """Maximum time between heartbeats."""

    retry_policy: Optional[RetryPolicy] = None
    """Policy for retrying failed activities."""

    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL
    """How the activity handles cancellation."""

    versioning_intent: Optional[VersioningIntent] = None
    """Versioning intent for the activity."""

    summary_override: Optional[
        Union[
            str,
            ModelSummaryProvider,
        ]
    ] = None
    """Summary for the activity execution."""

    priority: Priority = Priority.default
    """Priority for the activity execution."""
