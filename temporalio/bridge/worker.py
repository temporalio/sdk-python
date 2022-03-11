"""Worker using SDK Core."""

from dataclasses import dataclass
from typing import Iterable, List

import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.temporal_sdk_bridge
import temporalio.common
from temporalio.bridge.temporal_sdk_bridge import PollShutdownError


@dataclass
class WorkerConfig:
    """Python representation of the Rust struct for configuring a worker."""

    namespace: str
    task_queue: str
    max_cached_workflows: int
    max_outstanding_workflow_tasks: int
    max_outstanding_activities: int
    max_outstanding_local_activities: int
    max_concurrent_wft_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_at_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout_millis: int
    max_heartbeat_throttle_interval_millis: int
    default_heartbeat_throttle_interval_millis: int


class Worker:
    """SDK Core worker."""

    def __init__(
        self, client: temporalio.bridge.client.Client, config: WorkerConfig
    ) -> None:
        """Create SDK core worker with a client and config."""
        self._ref = temporalio.bridge.temporal_sdk_bridge.new_worker(
            client._ref, config
        )

    async def poll_workflow_activation(
        self,
    ) -> temporalio.bridge.proto.workflow_activation.WorkflowActivation:
        """Poll for a workflow activation."""
        return (
            temporalio.bridge.proto.workflow_activation.WorkflowActivation.FromString(
                await self._ref.poll_workflow_activation()
            )
        )

    async def poll_activity_task(
        self,
    ) -> temporalio.bridge.proto.activity_task.ActivityTask:
        """Poll for an activity task."""
        return temporalio.bridge.proto.activity_task.ActivityTask.FromString(
            await self._ref.poll_activity_task()
        )

    async def complete_workflow_activation(
        self,
        comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    ) -> None:
        """Complete a workflow activation."""
        await self._ref.complete_workflow_activation(comp.SerializeToString())

    async def complete_activity_task(
        self, comp: temporalio.bridge.proto.ActivityTaskCompletion
    ) -> None:
        """Complete an activity task."""
        await self._ref.complete_activity_task(comp.SerializeToString())

    def record_activity_heartbeat(
        self, comp: temporalio.bridge.proto.ActivityHeartbeat
    ) -> None:
        """Record an activity heartbeat."""
        self._ref.record_activity_heartbeat(comp.SerializeToString())

    def request_workflow_eviction(self, run_id: str) -> None:
        """Request a workflow be evicted."""
        self._ref.request_workflow_eviction(run_id)

    async def shutdown(self) -> None:
        """Shutdown the worker, waiting for completion."""
        await self._ref.shutdown()


def retry_policy_from_proto(
    p: temporalio.bridge.proto.common.RetryPolicy,
) -> temporalio.common.RetryPolicy:
    """Convert Core retry policy to Temporal retry policy."""
    return temporalio.common.RetryPolicy(
        initial_interval=p.initial_interval.ToTimedelta(),
        backoff_coefficient=p.backoff_coefficient,
        maximum_interval=p.maximum_interval.ToTimedelta()
        if p.HasField("maximum_interval")
        else None,
        maximum_attempts=p.maximum_attempts,
        non_retryable_error_types=p.non_retryable_error_types,
    )


def from_bridge_payloads(
    payloads: Iterable[temporalio.bridge.proto.common.Payload],
) -> List[temporalio.api.common.v1.Payload]:
    """Convert from bridge payloads to API payloads."""
    return [from_bridge_payload(p) for p in payloads]


def from_bridge_payload(
    payload: temporalio.bridge.proto.common.Payload,
) -> temporalio.api.common.v1.Payload:
    """Convert from a bridge payload to an API payload."""
    return temporalio.api.common.v1.Payload(
        metadata=payload.metadata, data=payload.data
    )


def to_bridge_payloads(
    payloads: Iterable[temporalio.api.common.v1.Payload],
) -> List[temporalio.bridge.proto.common.Payload]:
    """Convert from API payloads to bridge payloads."""
    return [to_bridge_payload(p) for p in payloads]


def to_bridge_payload(
    payload: temporalio.api.common.v1.Payload,
) -> temporalio.bridge.proto.common.Payload:
    """Convert from an API payload to a bridge payload."""
    return temporalio.bridge.proto.common.Payload(
        metadata=payload.metadata, data=payload.data
    )
