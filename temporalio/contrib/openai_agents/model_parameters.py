"""Parameters for configuring Temporal activity execution for model calls."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@dataclass
class ModelActivityParameters:
    """Parameters for configuring Temporal activity execution for model calls.

    This class encapsulates all the parameters that can be used to configure
    how Temporal activities are executed when making model calls through the
    OpenAI Agents integration.
    """

    task_queue: Optional[str] = None
    """Specific task queue to use for model activities."""

    schedule_to_close_timeout: Optional[timedelta] = timedelta(seconds=60)
    """Maximum time from scheduling to completion."""

    schedule_to_start_timeout: Optional[timedelta] = None
    """Maximum time from scheduling to starting."""

    start_to_close_timeout: Optional[timedelta] = None
    """Maximum time for the activity to complete."""

    heartbeat_timeout: Optional[timedelta] = None
    """Maximum time between heartbeats."""

    retry_policy: Optional[RetryPolicy] = None
    """Policy for retrying failed activities."""

    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL
    """How the activity handles cancellation."""

    versioning_intent: Optional[VersioningIntent] = None
    """Versioning intent for the activity."""

    summary_override: Optional[str] = None
    """Summary for the activity execution."""

    priority: Priority = Priority.default
    """Priority for the activity execution."""
